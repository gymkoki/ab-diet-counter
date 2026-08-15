/*
 * ABダイエット サービスワーカー
 *
 * 目的：サーバーが一瞬でも応答しないとき（デプロイ直後・ワーカー再起動・回線が細いとき）に
 *       「真っ白な画面が10秒」という状態を無くす。
 *
 * 方針：ネットワーク優先＋短いタイムアウト（NETWORK_TIMEOUT_MS）。
 *   - サーバーが元気なとき … これまで通り最新のHTMLを表示する（更新がすぐ反映される）
 *   - サーバーが遅い／落ちているとき … 前回の画面を即座に出す（白い画面にならない）
 *
 * 注意：
 *   - キャッシュするのはHTML本体（ナビゲーション）だけ。/api/ は絶対にキャッシュしない
 *     （Bカウントや体重が古い値で表示されると事故になるため）。
 *   - 万一このSWを止めたくなったら、このファイルの中身を
 *     self.addEventListener('install', () => self.registration.unregister());
 *     に差し替えてデプロイすれば全端末で自動的に解除される。
 */
const CACHE = 'ab-diet-shell-v1';
const NETWORK_TIMEOUT_MS = 1500;

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);

  // ネットワークを試す。ただし NETWORK_TIMEOUT_MS を過ぎたらキャッシュに切り替える。
  const network = fetch(request).then(async (res) => {
    // 正常なHTMLのときだけ保存する（500やメンテ画面を保存しない）
    if (res && res.ok && res.status === 200) {
      try { await cache.put(request, res.clone()); } catch (_) {}
    }
    return res;
  });

  const cached = await cache.match(request);
  if (!cached) return network;   // 初回は待つしかない

  const timeout = new Promise(resolve => setTimeout(() => resolve(null), NETWORK_TIMEOUT_MS));
  try {
    const res = await Promise.race([network.catch(() => null), timeout]);
    if (res) return res;
  } catch (_) {}

  // 遅い／失敗 → 前回の画面をすぐ出す。裏側では network がキャッシュを更新し続ける。
  return cached;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // APIは絶対にキャッシュしない（古い記録が出ると事故になる）
  if (url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate') {
    e.respondWith(networkFirst(req));
  }
});
