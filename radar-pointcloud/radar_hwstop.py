# -*- coding: utf-8 -*-
"""
硬件停机（**实验 · 命令模式连通性已实机验证；写寄存器停机未验证**）—— 让雷达芯片真正停止发射，从而降温。

背景
- 软件层「结束」（`radar_control.py`）只停止主机侧采集/解算/渲染，**芯片仍在持续
  发射调频连续波，温度不会下降**。
- DEV202H 固件 `App/common/src/cmdprocess.c` 支持串口下行命令写雷达寄存器
  （`WRITE_REG_CMD = 0x0001`，且必须在命令模式 `START_CFG_CMD = 0x00FF` 内执行），
  固件还自带「停机寄存器表」（`Middleware/common/src/banyan.c` 的
  `InitChipRegListStop_BanyanE`）。所以**理论上**可以用一条串口命令让芯片停下来。

本模块只负责「按固件解析规则把下行帧拼对」，并提供一个离线校验器；
寄存器**数值不写进本仓库**（厂商资料保密，AGENTS.md「保密与边界」）：
从仓库外的 JSON 读取，格式见 `docs/radar-hardware-stop-plan.md`。

帧格式（源自 `App/common/inc/cmdprocess.h` 的宏与 `cmdprocess.c` 的状态机）
    帧头 FD FC FB FA
    长度 u16LE        = 数据域字节数 = 2(cmdType) + N(cmdData)，必须 ≤ 54
    数据 cmdType u16LE + cmdData
    帧尾 04 03 02 01
  - `WRITE_REG_CMD` 的 cmdData = 设备地址 u16LE + N×（寄存器地址 u16LE, 值 u16LE）
    固件内部做 `devAddress = cmdData[0] << 1`，所以这里填 **7 位地址**。
    单帧最多 12 条寄存器：regNum = (len - 2 - 2)/4 ≤ (54-4)/4。
  - 应答帧：cmdType 或上 `0x0100`，数据域 2 字节状态。
    **状态约定要当心**（`masterProtocolVer == 0` → `FillCmdAck` 反转状态）：
    本机固件默认是**老约定 1=ACK_OK、0=ACK_FAIL**（见下方 ACK_OK_OLD 注释与
    `docs/radar-hardware-stop-plan.md` §7.1）。

**未验证项（不得当成结论）**
- 帧构造已与固件解析规则做**离线一致性检查**（`selftest.py` 测试 5）。
- **T1（进/出命令模式，不写寄存器）已于 2026-10-15 在 COM3 实机通过 9/9**：
  0x00FF/0x00FE 都收到 `ACK_OK`，命令模式期间上行几乎停流、退出后完全恢复（40960 B/s）。
  执行器见 `.deepworks/tmp/run_t1_hwstop.py`（本模块自带的 `--both-directions --send`
  每帧只 `read(16)` 一次，容易给出**假阴性**，不建议直接用于实机判定）。
- **仍未验证**：「写这些寄存器是否真的停机」「停机后能否完整恢复成 START 表」
  —— 一次都没在实机上试过。
- 进入命令模式后设备的上行数据流会受影响，务必**先停掉 `server.py`**再试。
"""
import argparse
import json
import os
import struct
import sys
import time

HEAD = b'\xfd\xfc\xfb\xfa'
TAIL = b'\x04\x03\x02\x01'
CMD_LEN_MAX = 64
CMD_BUF_LEN = CMD_LEN_MAX - len(HEAD) - len(TAIL) - 2      # 54：数据域上限
MAX_REGS_PER_FRAME = (CMD_BUF_LEN - 2 - 2) // 4            # 12

START_CFG_CMD = 0x00FF
FINISH_CFG_CMD = 0x00FE
WRITE_REG_CMD = 0x0001
READ_REG_CMD = 0x0002
ACK_TYPE = 0x0100
ACK_OK = 0
ACK_FAIL = 1
# 老协议约定（固件 cmdprocess.c L122-143 + cmdprocess.h L122-126）：
# masterProtocolVer == 0 时，FillCmdAck 会把状态**反转**成 ACK_OK_OLD=1 / ACK_FAIL_OLD=0。
# 本机固件启动时 masterProtocolVer 默认 0，而本工具发的 0x00FF 不带数据域
# （cmdLen == CMD_TYPE_LEN）→ 固件走 else 分支显式置 masterProtocolVer = 0，
# 所以实机收到的应答状态就是**老约定：1 = 成功、0 = 失败**。
# 2026-10-15 T1 实测：0x00FF / 0x00FE 的应答都是 type=命令|0x0100、len=4、status=1 → 两者都是 ACK_OK。
ACK_OK_OLD = 1
ACK_FAIL_OLD = 0

DEFAULT_PLAN = os.path.join(os.path.expanduser('~'), '.deepworks',
                            'radar-hwstop-plan.json')

UNVERIFIED_WARNING = (
    '!! 未验证功能：本工具只保证「下行帧与固件解析规则一致」（离线验证），\n'
    '   不保证写这些寄存器真的会停机、也不保证之后能完整恢复。\n'
    '   首次实机尝试请：① 先停掉 server.py（释放 COM3）② 手边能重新插拔 USB\n'
    '   ③ 停机后立刻做一次 START 恢复验证。')


def frame(cmd_type, data=b''):
    """按固件 CmdProcess 规则拼一帧下行命令。"""
    payload = struct.pack('<H', cmd_type) + bytes(data)
    if len(payload) > CMD_BUF_LEN:
        raise ValueError('数据域 %d 字节超过上限 %d（固件 len 检查会直接丢弃）'
                         % (len(payload), CMD_BUF_LEN))
    return HEAD + struct.pack('<H', len(payload)) + payload + TAIL


def write_reg_frames(dev_addr7, regs):
    """把 (寄存器地址, 值) 列表切成多帧 WRITE_REG_CMD（每帧 ≤12 条）。"""
    regs = [(int(a) & 0xFFFF, int(v) & 0xFFFF) for a, v in regs]
    out = []
    for i in range(0, len(regs), MAX_REGS_PER_FRAME):
        chunk = regs[i:i + MAX_REGS_PER_FRAME]
        data = struct.pack('<H', int(dev_addr7) & 0xFF)
        data += b''.join(struct.pack('<HH', a, v) for a, v in chunk)
        out.append(frame(WRITE_REG_CMD, data))
    return out


def build_plan_frames(steps):
    """steps: [{'dev': 7位地址, 'regs': [[addr, val], ...], 'label': str}, ...]

    返回 [(label, 帧)]，自动在最前/最后加进入/退出命令模式的帧。
    """
    frames = [('进入命令模式 0x00FF', frame(START_CFG_CMD))]
    for st in steps:
        dev = int(st['dev'])
        regs = st.get('regs') or []
        label = st.get('label') or ('写寄存器 dev=0x%02X ×%d' % (dev, len(regs)))
        for f in write_reg_frames(dev, regs):
            frames.append((label, f))
    frames.append(('退出命令模式 0x00FE', frame(FINISH_CFG_CMD)))
    return frames


def load_plan(path):
    with open(path, 'r', encoding='utf-8') as f:
        obj = json.load(f)
    steps = obj.get('steps')
    if not steps:
        raise SystemExit('计划文件缺少 "steps" 字段: %s' % path)
    return obj, steps


# ---------------------------------------------------------------- 离线校验器
class FirmwareChecker:
    """按固件 CmdProcess 状态机复刻的校验器（离线用）。

    用来证明「我们拼的帧，固件那条状态机能原样解析出来」——
    这是本模块唯一能离线验证的部分。
    """

    def __init__(self):
        self._buf = bytearray()
        self.state = 0
        self.len = 0
        self.errors = 0
        self.cmds = []

    def feed(self, data):
        for b in data:
            self.state = self._step(b)
        return self.cmds

    def _step(self, b):
        # 0..3 帧头；4..5 长度；6 数据；7..10 帧尾
        if self.state < 4:
            if b == HEAD[self.state]:
                self.state += 1
            else:
                self.errors += 1
                self.state = 1 if b == HEAD[0] else 0
            return self.state
        if self.state == 4:
            self.len = b
            self._buf = bytearray([b])
            return 5
        if self.state == 5:
            self.len += b << 8
            if self.len <= CMD_BUF_LEN:
                self._buf.append(b)
                return 6
            self.errors += 1
            return 0
        if self.state == 6:
            self._buf.append(b)
            if len(self._buf) == self.len + 2:
                return 7
            return 6
        if 7 <= self.state <= 9:
            if b == TAIL[self.state - 7]:
                self._buf.append(b)
                return self.state + 1
            self.errors += 1
            return 0
        if self.state == 10:
            if b == TAIL[3]:
                self._buf.append(b)
                self._emit()
                return 0
            self.errors += 1
            return 0
        return 0

    def _emit(self):
        payload = bytes(self._buf[2:2 + self.len])
        cmd_type = struct.unpack('<H', payload[:2])[0]
        self.cmds.append({'type': cmd_type, 'len': self.len, 'data': payload[2:]})


def decode_write_reg(cmd):
    """把一条 PARSE 出来的 WRITE_REG 命令还原成 (7位设备地址, [(addr, val)])。"""
    data = cmd['data']
    if len(data) < 2:
        raise ValueError('数据域太短')
    dev7 = struct.unpack('<H', data[:2])[0]
    regs = []
    for i in range(2, len(data) - 3, 4):
        regs.append(struct.unpack('<HH', data[i:i + 4]))
    return dev7, regs


def parse_ack(frame_bytes, master_protocol_ver=0):
    """解析应答帧 → dict（含原始状态与「按约定解释后」的 ok）。

    master_protocol_ver=0（默认）＝**老约定**：1=ACK_OK、0=ACK_FAIL。
    这正是一台固件启动后的默认状态，也是本工具 `--both-directions`
    （0x00FF 不带数据域）所触发的状态，所以默认值取 0。
    只有显式在 0x00FF 的数据域里塞了非 0 协议版本时，才是新约定（0=OK、1=FAIL）。
    """
    chk = FirmwareChecker()
    cmds = chk.feed(frame_bytes)
    if not cmds:
        return None
    c = cmds[0]
    status = struct.unpack('<H', c['data'][:2])[0] if len(c['data']) >= 2 else None
    ok_val = ACK_OK if master_protocol_ver else ACK_OK_OLD
    fail_val = ACK_FAIL if master_protocol_ver else ACK_FAIL_OLD
    if status == ok_val:
        text = 'ACK_OK'
    elif status == fail_val:
        text = 'ACK_FAIL'
    else:
        text = '?'
    return {'type': c['type'] & ~ACK_TYPE, 'raw_type': c['type'], 'status': status,
            'status_text': text, 'ok': text == 'ACK_OK',
            'convention': 'new' if master_protocol_ver else 'old'} 


# --------------------------------------------------------------------- 执行
def dry_run(frames, show_all=True):
    print(UNVERIFIED_WARNING)
    print('\n[dry-run] 共 %d 帧，不打开任何串口：\n' % len(frames))
    for i, (label, f) in enumerate(frames, 1):
        chk = FirmwareChecker()
        cmds = chk.feed(f)
        ok = len(cmds) == 1 and chk.errors == 0
        kind = ''
        if ok:
            c = cmds[0]
            if c['type'] == WRITE_REG_CMD:
                dev, regs = decode_write_reg(c)
                kind = 'WRITE_REG dev7=0x%02X ×%d' % (dev, len(regs))
                if show_all:
                    kind += '  ' + ', '.join('0x%02X=0x%04X' % r for r in regs)
            elif c['type'] == START_CFG_CMD:
                kind = 'START_CFG'
            elif c['type'] == FINISH_CFG_CMD:
                kind = 'FINISH_CFG'
        print('%3d. %-26s len=%2d  %s  自检=%s' %
              (i, label, len(f) - 10, kind, '通过' if ok else '失败'))
        print('     %s' % f.hex(' '))
    print('\n[dry-run] 结束。确认无误后再加 --send 实机执行（会先要求确认）。')


def send(frames, port, baud=921600, timeout=1.0, echo=True):
    import serial
    print(UNVERIFIED_WARNING)
    print('\n即将向 %s@%d 写入 %d 帧下行命令。' % (port, baud, len(frames)))
    print('注意：进入命令模式后设备上行数据流会受影响；请确认 server.py 已停止。')
    ser = serial.Serial(port, baud, timeout=timeout)
    results = []
    try:
        ser.reset_input_buffer()
        for label, f in frames:
            ser.write(f)
            ser.flush()
            time.sleep(0.05)
            resp = ser.read(16)
            ack = parse_ack(resp) if resp else None
            results.append({'label': label, 'sent': f.hex(' '),
                            'resp': resp.hex(' ') if resp else None, 'ack': ack})
            if echo:
                print('  %-26s → %s' % (label, ack if ack else '无应答'))
            time.sleep(0.15)
    finally:
        ser.close()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description='雷达硬件停机命令构造（实验 · 未验证）')
    ap.add_argument('--plan', default=DEFAULT_PLAN,
                    help='停机计划 JSON（默认 %s，故意放在仓库外）' % DEFAULT_PLAN)
    ap.add_argument('--dev', type=lambda s: int(s, 0), default=None,
                    help='只发单个设备的简单模式：7 位设备地址')
    ap.add_argument('--reg', action='append', default=[],
                    help='简单模式寄存器 addr=val，可重复（addr/val 支持 0x 前缀）')
    ap.add_argument('--both-directions', action='store_true',
                    help='只做进入/退出命令模式的连通性试探（不写任何寄存器）')
    ap.add_argument('--send', action='store_true', help='真的往串口写（默认只 dry-run）')
    ap.add_argument('--port', default='COM3')
    ap.add_argument('--baud', type=int, default=921600)
    ap.add_argument('--confirm', action='store_true',
                    help='实机写入必须显式确认：我明白这是未验证功能')
    args = ap.parse_args(argv)

    if args.both_directions:
        frames = [('进入命令模式 0x00FF', frame(START_CFG_CMD)),
                  ('退出命令模式 0x00FE', frame(FINISH_CFG_CMD))]
    elif args.reg:
        if args.dev is None:
            raise SystemExit('简单模式需要 --dev <7位地址>')
        regs = []
        for item in args.reg:
            a, _, v = item.partition('=')
            regs.append((int(a, 0), int(v, 0)))
        frames = build_plan_frames([{'dev': args.dev, 'regs': regs, 'label': '写寄存器'}])
    else:
        if not os.path.exists(args.plan):
            print('未找到计划文件: %s\n' % args.plan)
            print('格式（放到仓库外，勿提交寄存器数值）:')
            print(json.dumps({'note': '寄存器值来自厂商资料，不要提交进 git',
                              'steps': [{'dev': 9, 'label': 'STOP 停机表',
                                         'regs': [[64, 16903], [65, 0]]}]},
                             ensure_ascii=False, indent=2))
            print('\n提示：只想试「命令模式连通性」可加 --both-directions（不写寄存器）。')
            return 2
        _, steps = load_plan(args.plan)
        frames = build_plan_frames(steps)

    if not args.send:
        dry_run(frames)
        return 0

    if not args.confirm:
        print(UNVERIFIED_WARNING)
        print('\n拒绝执行：这是未验证功能，必须加 --confirm 表示「我明白风险」才能实机写入。')
        return 3
    res = send(frames, args.port, args.baud)
    ok = sum(1 for r in res if r['ack'] and r['ack']['ok'])
    print('\n完成：%d/%d 帧收到 ACK_OK（本固件默认老约定：status=1 为 ACK_OK）'
          % (ok, len(res)))
    return 0 if ok == len(res) else 4


if __name__ == '__main__':
    sys.exit(main())
