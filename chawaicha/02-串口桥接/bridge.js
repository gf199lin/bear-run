// ============================================================
// 雷达串口桥接助手 (路线B)
// 作用: 用大缓冲区专职读雷达串口, 通过本地 WebSocket 把字节转发给网页。
// 浏览器不再直接碰串口, 卡死/overrun 问题从根上消失。
// 用法:   双击 start_bridge.bat   (或: node bridge.js [COM口])
//        双板同时用: node bridge.js --com=COM9 --baud=1500000 --ws=8765   (DEV113P 跳/蹲)
//                    node bridge.js --com=COM8 --baud=921600  --ws=8768   (DEV202H 左右)
// 关闭:   直接关这个窗口 = 断开雷达
// ============================================================
const path = require('path');
// 依赖装在 _work/node_modules (不刷屏), 从那里解析
const localRequire = require('module').createRequire(path.join(__dirname, '_work', 'node_modules', 'index.js'));
const { SerialPort } = localRequire('serialport');
const { WebSocketServer } = localRequire('ws');

// v1.1: 支持命令行参数, 让两块板子各自跑一个实例(不同串口/波特率/网页端口)
function opt(name, def){
  const p = '--' + name + '=';
  for(const a of process.argv.slice(2)){ if(a.indexOf(p) === 0) return a.slice(p.length); }
  return def;
}
const COM_ARG = opt('com', '');
const BAUD = parseInt(opt('baud', '1500000'), 10);   // DEV113P=1500000 / DEV202H=921600
const WS_PORT = parseInt(opt('ws', '8765'), 10);    // 网页连这个地址: ws://127.0.0.1:8765

let port = null;
let opening = false;
const clients = new Set();
let rxBytes = 0, lastRx = 0;

// ---- 自动挑选串口: 先排除蓝牙虚拟串口(名字也带"串行"会抢匹配), 再优先 USB/STM/CH340/FTDI/Serial; 也可命令行指定 ----
async function pickPath(){
  const arg = COM_ARG || (process.argv[2] && process.argv[2].charAt(0) !== '-' ? process.argv[2] : '');
  if(arg) return arg;
  const ports = await SerialPort.list();
  console.log('[扫描] 发现的串口:');
  ports.forEach(p => console.log('   ', p.path, '-', p.friendlyName || p.manufacturer || '(未知设备)'));
  if(!ports.length) throw new Error('一个串口都没有——请插好雷达板再试');
  const noBt = ports.filter(p => !/蓝牙|bluetooth/i.test((p.friendlyName || '') + ' ' + (p.manufacturer || '')));
  const pool = noBt.length ? noBt : ports;
  const hit = pool.find(p => /usb|stm|serial|ch340|ftdi|串行/i.test((p.friendlyName || '') + ' ' + (p.manufacturer || '')));
  const picked = hit || pool[0];
  console.log(`[选口] 自动选择 ${picked.path} (${picked.friendlyName || '未知设备'})${noBt.length < ports.length ? ' · 已排除蓝牙虚拟串口' : ''}`);
  return picked.path;
}

async function openSerial(){
  if(opening) return;
  opening = true;
  try{
    const p = await pickPath();
    console.log(`[串口] 打开 ${p} @${BAUD} …`);
    port = new SerialPort({
      path: p, baudRate: BAUD, dataBits: 8, stopBits: 1, parity: 'none',
      highWaterMark: 1 << 20,   // 1MB 读缓冲 —— 这就是治本的底气
      autoOpen: false,
    });
    // 串口 → 所有已连接的网页
    port.on('data', chunk => {
      rxBytes += chunk.length;
      for(const c of clients){
        if(c.readyState === 1 && c.bufferedAmount < (4 << 20)) c.send(chunk, { binary: true });
      }
    });
    port.on('close', () => {
      console.log('[串口] 断开了, 2秒后自动重连…');
      port = null;
      setTimeout(openSerial, 2000);
    });
    port.on('error', e => console.log('[串口] 错误:', e.message));
    await new Promise((res, rej) => port.open(e => e ? rej(e) : res()));
    // 与厂商工具一致: 打开后置 DTR/RTS (板子的命令解析受它门控)
    try{ await new Promise(res => port.set({ dtr: true, rts: true }, () => res())); }catch(e){}
    console.log('[串口] 已打开, 数据畅通 ✓');
  }catch(e){
    console.log('[串口] 打开失败:', e.message);
    console.log('       3秒后重试 (请确认: 板子已插好 / 没被厂商工具或网页直连占用)');
    port = null;
    setTimeout(openSerial, 3000);
  }finally{
    opening = false;
  }
}

// ---- WebSocket 服务: 等网页来连 ----
const wss = new WebSocketServer({ port: WS_PORT });
wss.on('listening', () => console.log(`[桥接] 网页通道就绪: ws://127.0.0.1:${WS_PORT}`));
wss.on('connection', ws => {
  clients.add(ws);
  console.log(`[桥接] 网页连上了 (当前 ${clients.size} 个)`);
  // 网页 → 串口 (配置命令等)
  ws.on('message', data => { if(port) port.write(Buffer.from(data)); });
  ws.on('close', () => { clients.delete(ws); console.log(`[桥接] 网页断开 (剩 ${clients.size} 个)`); });
  ws.on('error', () => {});
});

// ---- 每10秒打个心跳, 一眼看出通没通 ----
setInterval(() => {
  const kb = ((rxBytes - lastRx) / 10240).toFixed(1);
  lastRx = rxBytes;
  console.log(`[心跳] 累计 ${(rxBytes / 1048576).toFixed(1)}MB · ${kb}KB/s · 网页${clients.size}个 · 串口${port ? '开' : '关'}`);
}, 10000);

console.log('============================================');
console.log(`  雷达桥接助手 v1.1  ws:${WS_PORT} baud:${BAUD} ${COM_ARG ? 'com:' + COM_ARG : '(自动选口)'}  (这个窗口要一直开着)`);
console.log('============================================');
openSerial();
