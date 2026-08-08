// MOM 베이커리 서비스워커 — 정적 파일만 캐시, API는 항상 네트워크
const CACHE = 'mom-bakery-v3';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  // 예전 판 캐시는 지우고 바로 새 것을 쓴다 (홈 화면 앱이 옛 화면에 머무는 걸 막는다)
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
        .then(() => clients.claim())
));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  // 화면(HTML)은 브라우저 캐시를 건너뛰고 항상 새로 받아 온다. 인터넷이 없을 때만 캐시를 쓴다.
  const isPage = e.request.mode === 'navigate' ||
                 (e.request.headers.get('accept') || '').includes('text/html');
  e.respondWith(
    fetch(isPage ? new Request(e.request, {cache: 'reload'}) : e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request))
  );
});

// 웹 푸시 알림 수신
self.addEventListener('push', e => {
  let data = {};
  try { data = e.data.json(); } catch(_) { data = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(data.title || '🥐 MOM 베이커리', {
    body: data.body || '오늘의 묵상 시간이에요',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: data.url || '/' }
  }));
});

// 알림 탭 → 앱 열기
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) { if (c.url.includes(url) && 'focus' in c) return c.focus(); }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
