// ═══════════════════════════════════════════
//  管理画面の認証（スマホ・パソコン共通）
//  Basic認証ダイアログはスマホ（特にホーム画面に追加したアプリ）で
//  正しく動かないことが多いため、ページ内のログインフォームで
//  パスワードを入力してもらい、端末に保存して以後のAPIリクエストに
//  X-Admin-Password ヘッダーとして自動で付与する。
// ═══════════════════════════════════════════
(function () {
  const KEY = 'ab-admin-pw';

  // すべての fetch に管理パスワードのヘッダーを自動付与
  const origFetch = window.fetch.bind(window);
  window.fetch = (url, opts = {}) => {
    const pw = localStorage.getItem(KEY) || '';
    if (pw) opts.headers = Object.assign({}, opts.headers, { 'X-Admin-Password': pw });
    return origFetch(url, opts);
  };

  let overlay = null;

  function buildOverlay() {
    overlay = document.createElement('div');
    overlay.id = 'admin-login-overlay';
    overlay.style.cssText =
      'position:fixed;inset:0;z-index:9999;background:#F3F4F6;display:flex;' +
      'align-items:center;justify-content:center;padding:24px';
    overlay.innerHTML = `
      <div style="background:#fff;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.12);
                  padding:28px 24px;max-width:360px;width:100%;text-align:center">
        <div style="font-size:34px;margin-bottom:8px">🔐</div>
        <div style="font-size:17px;font-weight:800;color:#111827;margin-bottom:6px">管理画面ログイン</div>
        <div style="font-size:13px;color:#6B7280;margin-bottom:18px;line-height:1.6">
          管理者パスワードを入力してください。<br>一度入力すれば、この端末では次回から不要です。</div>
        <input type="password" id="admin-login-pw" placeholder="パスワード" autocomplete="current-password"
               style="width:100%;padding:12px 14px;border:1.5px solid #E5E7EB;border-radius:10px;
                      font-size:16px;outline:none;margin-bottom:12px;box-sizing:border-box">
        <button id="admin-login-btn"
                style="width:100%;padding:12px;border:none;border-radius:10px;cursor:pointer;
                       background:linear-gradient(135deg,#FF6B35,#e55a25);color:#fff;
                       font-size:15px;font-weight:800">ログイン</button>
        <div id="admin-login-err" style="display:none;color:#DC2626;font-size:13px;
             font-weight:700;margin-top:10px"></div>
      </div>`;
    document.body.appendChild(overlay);
  }

  async function tryAuth() {
    try {
      const r = await fetch('/api/admin/auth-check');
      return r.ok;
    } catch (e) {
      return false;
    }
  }

  function showLogin(resolve) {
    if (!overlay) buildOverlay();
    overlay.style.display = 'flex';
    const input = document.getElementById('admin-login-pw');
    const btn   = document.getElementById('admin-login-btn');
    const err   = document.getElementById('admin-login-err');
    input.focus();

    async function submit() {
      const pw = input.value;
      if (!pw) { input.focus(); return; }
      btn.disabled = true;
      btn.textContent = '確認中…';
      localStorage.setItem(KEY, pw);
      const ok = await tryAuth();
      btn.disabled = false;
      btn.textContent = 'ログイン';
      if (ok) {
        overlay.style.display = 'none';
        resolve(true);
      } else {
        localStorage.removeItem(KEY);
        err.textContent = 'パスワードが違います';
        err.style.display = 'block';
        input.value = '';
        input.focus();
      }
    }
    btn.onclick = submit;
    input.onkeydown = e => { if (e.key === 'Enter') submit(); };
  }

  window.adminAuth = {
    // 認証が通るまで解決しない。ページの初期読み込み前に await して使う。
    ensure() {
      return tryAuth().then(ok => ok || new Promise(showLogin));
    },
  };
})();
