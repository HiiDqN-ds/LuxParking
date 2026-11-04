(function(){
  var root=document.querySelector('[data-lot-tour]'); if(!root) return;
  var scenes=[].slice.call(root.querySelectorAll('[data-scene]')),
      steps=[].slice.call(root.querySelectorAll('[data-go]')),
      ui=root.querySelector('[data-ui]'), nav=root.querySelector('[data-steps]'),
      playBtn=root.querySelector('[data-play-btn]'), i=0, timer=null,
      still=matchMedia('(prefers-reduced-motion: reduce)').matches, playing=!still, DUR=6500;
  root.classList.add('is-tour'); ui.hidden=false; nav.hidden=false;
  function show(n){
    i=(n+scenes.length)%scenes.length;
    scenes.forEach(function(s,k){s.classList.toggle('is-on',k===i); s.setAttribute('aria-hidden',k===i?'false':'true');});
    steps.forEach(function(b,k){k===i?b.setAttribute('aria-current','true'):b.removeAttribute('aria-current');});
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
