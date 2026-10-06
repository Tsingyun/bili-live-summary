// login_by_qr.js —— B站扫码登录，导出投稿用 Netscape cookie
//
// 用途：upload.backend=api 靠 SESSDATA / bili_jct / DedeUserID 三件套维持登录态。
//      B站会话过期后上传会在 member.bilibili.com/preupload 报 HTTP 403，
//      而本机 Chrome 154 起 cookie 用 App-Bound 加密（v20），沙箱内无法解密，
//      所以走「独立浏览器 + 手机扫码」这条路取凭据。
//
// 用法：
//   node login_by_qr.js [二维码png路径] [cookie输出路径]
//   默认二维码 C:/Users/Tsing/AppData/Local/Temp/bili_qr.png
//        cookie  ~/.bilibili_small_cookies.txt（即 config.upload.cookies_path）
//
// 前置：playwright + chromium 已装在 managed node workspace
//   cd C:/Users/Tsing/AppData/Local/Temp &&
//   NODE_PATH=C:/Users/Tsing/.workbuddy/binaries/node/workspace/node_modules \
//   C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-3/node.exe login_by_qr.js
//
// ★ 三个实测坑（别回退）：
//   1) 直接 goto passport h5 页会跳到「二维码已失效」页 → 必须先访问
//      www.bilibili.com 拿到 buvid，再调 /x/passport-login/web/qrcode/generate。
//   2) 凭据在 poll 成功响应的 data.url query 里（SESSDATA=...），
//      **不是** ctx.cookies()（HttpOnly 且域隔离，context 里取不到）。
//   3) poll 未登录时也返回 code=0 但 data.url 为空 → 判据必须是「三项凭据齐全」，
//      不能只看 code==0。二维码截图要用 deviceScaleFactor:3 才扫得出来。

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const os = require('os');

const OUT = process.argv[2]
  || path.join(os.tmpdir(), 'bili_qr.png');
const CK = process.argv[3]
  || path.join(os.homedir(), '.bilibili_small_cookies.txt');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36';
const NEED = ['SESSDATA', 'bili_jct', 'DedeUserID'];
const WAIT_MS = 6 * 60 * 1000;

function qs(u) {
  const out = {};
  try {
    new URL(u).search.replace(/^\?/, '').split('&').forEach(kv => {
      if (!kv) return;
      const i = kv.indexOf('=');
      if (i > 0) out[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
    });
  } catch (e) { /* 非 URL 就忽略 */ }
  return out;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    userAgent: UA, viewport: { width: 860, height: 1000 }, deviceScaleFactor: 3,
  });
  const page = await ctx.newPage();
  try {
    // 坑1：先访问主站拿前置 cookie
    await page.goto('https://www.bilibili.com/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(1500);

    const gen = await page.request.get(
      'https://passport.bilibili.com/x/passport-login/web/qrcode/generate',
      { headers: { referer: 'https://www.bilibili.com/' } });
    const gj = await gen.json();
    if (!gj || gj.code !== 0) throw new Error('generate code=' + (gj && gj.code));
    const qrUrl = gj.data.url, key = gj.data.qrcode_key;
    console.log('qrcode_key=' + key);

    await page.goto(qrUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(2500);
    // 坑3：整页截图 + 3x 像素比，确保手机能扫
    const el = await page.$('.qrcode-img') || await page.$('.login-scan-box img');
    if (el) await el.screenshot({ path: OUT });
    else await page.screenshot({ path: OUT, fullPage: true });
    console.log('QR saved: ' + OUT);
    console.log('等待扫码（6 分钟）...');

    const deadline = Date.now() + WAIT_MS;
    let done = false, lastMsg = '';
    while (Date.now() < deadline && !done) {
      await page.waitForTimeout(3000);
      const poll = await page.request.get(
        'https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=' + key,
        { headers: { referer: qrUrl } });
      const pj = await poll.json();
      const code = pj && pj.code;
      if (code === 0) {
        // 坑2+3：凭据在 data.url；code=0 但空 data 不算成功
        const map = {};
        const q = qs((pj.data && pj.data.url) || '');
        for (const k of NEED) if (q[k]) map[k] = q[k];
        for (const c of await ctx.cookies()) {
          if (NEED.includes(c.name) && !map[c.name] && c.value) map[c.name] = c.value;
        }
        const missing = NEED.filter(k => !map[k]);
        if (missing.length) {
          const msg = 'code=0 但缺 ' + missing.join(',')
            + '（data.url keys=' + Object.keys(q).join(',') + '）';
          if (msg !== lastMsg) { console.log(msg); lastMsg = msg; }
          continue;
        }
        // 核身份：确认登的是「小号」而不是主号
        let who = 'uid=' + map.DedeUserID;
        try {
          const r = await page.request.get(
            'https://api.bilibili.com/x/space/acc/info?mid=' + map.DedeUserID,
            { headers: { cookie: NEED.map(k => k + '=' + map[k]).join('; ') } });
          const j = await r.json();
          who = (j && j.code === 0 && j.data)
            ? j.data.name + ' / ' + who
            : '(身份未核出) ' + who;
        } catch (e) { who = '(身份探测失败) ' + who; }

        if (fs.existsSync(CK)) {
          fs.copyFileSync(CK, CK + '.bak_' + new Date().toISOString().slice(0, 10));
          console.log('旧 cookie 已备份');
        }
        const lines = ['# Netscape HTTP Cookie File',
          '# 由 login_by_qr.js（Playwright 扫码登录）生成，请勿外传', ''];
        for (const k of NEED) lines.push('\t.bilibili.com\tTRUE\t/\tTRUE\t0\t' + k + '\t' + map[k]);
        fs.writeFileSync(CK, lines.join('\n') + '\n', 'utf8');
        console.log('LOGIN OK -> ' + who);
        console.log('SESSDATA 已加载（长度 ' + map.SESSDATA.length + '）');
        console.log('cookie 文件：' + CK);
        done = true;
      } else if (code === 86101) {
        /* 未扫码，静默等 */
      } else if (code === 86038) {
        if (lastMsg !== 'scanned') { console.log('已扫码，待手机确认'); lastMsg = 'scanned'; }
      } else if (code === 86090) {
        console.log('二维码已失效，请重跑本脚本'); break;
      } else {
        console.log('poll code=' + code + ' ' + JSON.stringify(pj.data || {}).slice(0, 100));
      }
    }
    if (!done) { console.log('TIMEOUT 未完成登录'); process.exitCode = 3; }
  } catch (e) {
    console.log('ERROR: ' + e.message.split('\n')[0]);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
