try{var t=localStorage.getItem("ff_theme");if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.dataset.theme=t}catch(e){}

document.addEventListener("click",function(e){var b=e.target.closest&&e.target.closest("[data-toggle-pass]");if(!b)return;var i=document.getElementById(b.dataset.togglePass);if(!i)return;var show=i.type==="password";i.type=show?"text":"password";b.textContent=show?"Hide":"Show"});

if("serviceWorker" in navigator&&(location.protocol==="https:"||location.hostname==="localhost"||location.hostname==="127.0.0.1")){addEventListener("load",function(){navigator.serviceWorker.register("/sw.js").catch(function(){})})}
