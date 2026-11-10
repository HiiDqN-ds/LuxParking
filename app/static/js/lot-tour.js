(function(){
  var root=document.querySelector('[data-lot-tour]'); if(!root) return;
  var scenes=[].slice.call(root.querySelectorAll('[data-scene]')),
      steps=[].slice.call(root.querySelectorAll('[data-go]')),
      ui=root.querySelector('[data-ui]'), cap=root.querySelector('[data-cap]'), nav=root.querySelector('[data-steps]'),
      playBtn=root.querySelector('[data-play-btn]'), i=0, timer=null,
      still=matchMedia('(prefers-reduced-motion: reduce)').matches, playing=!still, DUR=6500;
  root.classList.add('is-tour'); ui.hidden=false; cap.hidden=false; nav.hidden=false;
  function show(n){
    i=(n+scenes.length)%scenes.length;
    scenes.forEach(function(s,k){s.classList.toggle('is-on',k===i); s.setAttribute('aria-hidden',k===i?'false':'true');});
    steps.forEach(function(b,k){k===i?b.setAttribute('aria-current','true'):b.removeAttribute('aria-current');});
    cap.innerHTML=scenes[i].querySelector('figcaption').innerHTML; // server-rendered, already escaped
    var nx=scenes[(i+1)%scenes.length].querySelector('img'); if(nx) nx.loading='eager'; // preload next
  }
  function tick(){clearTimeout(timer); if(playing) timer=setTimeout(function(){show(i+1);tick();},DUR);}
  function setPlay(p){playing=p; root.classList.toggle('is-paused',!p); playBtn.setAttribute('aria-pressed',p); playBtn.textContent=p?root.dataset.pause:root.dataset.play; tick();}
  root.querySelector('[data-next]').onclick=function(){show(i+1);tick();};
  root.querySelector('[data-prev]').onclick=function(){show(i-1);tick();};
  steps.forEach(function(b){b.onclick=function(){show(+b.dataset.go);tick();};});
  playBtn.onclick=function(){setPlay(!playing);};
  root.addEventListener('keydown',function(e){if(e.key==='ArrowRight'){show(i+1);tick();}if(e.key==='ArrowLeft'){show(i-1);tick();}});
  // pause when off-screen (saves battery, avoids wasted motion)
  if('IntersectionObserver' in window) new IntersectionObserver(function(en){en[0].isIntersecting?tick():clearTimeout(timer);}).observe(root);
  show(0); setPlay(playing);
})();

// gallery lightbox
(function(){
  var dlg=document.querySelector('[data-lightbox]'); if(!dlg||typeof dlg.showModal!=='function') return;
  var links=[].slice.call(document.querySelectorAll('.lg-grid a')); if(!links.length) return;
  var img=dlg.querySelector('[data-img]'), cnt=dlg.querySelector('[data-count]'), i=0, x0=null;
  function show(n){
    i=(n+links.length)%links.length; var a=links[i];
    img.src=a.href; img.alt=a.querySelector('img').alt; cnt.textContent=(i+1)+' / '+links.length;
    [1,-1].forEach(function(d){var p=new Image(); p.src=links[(i+d+links.length)%links.length].href;}); // preload neighbours
  }
  links.forEach(function(a,k){a.addEventListener('click',function(e){
    e.preventDefault(); show(k); dlg.showModal(); history.pushState({lb:1},'');
  });});
  dlg.querySelector('[data-close]').addEventListener('click',function(){dlg.close();});
  dlg.querySelector('[data-prev]').addEventListener('click',function(){show(i-1);});
  dlg.querySelector('[data-next]').addEventListener('click',function(){show(i+1);});
  dlg.addEventListener('click',function(e){if(e.target===dlg) dlg.close();});
  dlg.addEventListener('keydown',function(e){if(e.key==='ArrowRight') show(i+1); if(e.key==='ArrowLeft') show(i-1);});
  dlg.addEventListener('touchstart',function(e){x0=e.touches[0].clientX;},{passive:true});
  dlg.addEventListener('touchend',function(e){
    if(x0===null) return; var dx=e.changedTouches[0].clientX-x0; x0=null;
    if(Math.abs(dx)>50) show(dx<0?i+1:i-1);
  });
  // the browser/phone back button closes the viewer instead of leaving the page
  window.addEventListener('popstate',function(){if(dlg.open) dlg.close();});
  dlg.addEventListener('close',function(){if(history.state&&history.state.lb) history.back();});
})();
