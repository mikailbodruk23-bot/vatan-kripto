// VATAN 2 - yalnızca statik arayüz için önbellek; Binance API yanıtları önbelleğe alınmaz.
const CACHE = 'vatan2-shell-20261106-v1';
const SHELL = ['./index.html','./vatan2-mobil-panel-v11.html','./vatan1-manuel.html','./icon.png','./manifest.webmanifest'];
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>Promise.allSettled(SHELL.map(url=>cache.add(url)))).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
  event.waitUntil((async()=>{
    const names=await caches.keys();
    await Promise.all(names.filter(n=>n.startsWith('vatan-')||n.startsWith('vatan2-shell-')).filter(n=>n!==CACHE).map(n=>caches.delete(n)));
    await self.clients.claim();
  })());
});
self.addEventListener('fetch',event=>{
  const req=event.request; if(req.method!=='GET')return;
  const url=new URL(req.url);
  // Canlı borsa ve harici kaynaklarda Service Worker devreye girmez.
  if(url.origin!==self.location.origin)return;
  if(req.mode==='navigate' || /\.(html|webmanifest)$/.test(url.pathname)){
    event.respondWith((async()=>{
      try{const fresh=await fetch(req);if(fresh.ok){const cache=await caches.open(CACHE);cache.put(req,fresh.clone());}return fresh;}
      catch(e){return (await caches.match(req,{ignoreSearch:true})) || new Response('Bağlantı yok: VATAN 2 sayfası yüklenemedi.',{status:503,headers:{'Content-Type':'text/plain; charset=utf-8'}});}
    })());
  } else if(url.pathname.endsWith('/icon.png')){
    event.respondWith(caches.match(req).then(hit=>hit||fetch(req)));
  }
});
