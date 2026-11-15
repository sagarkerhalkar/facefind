try{var t=localStorage.getItem("ff_theme");if(!t)t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.dataset.theme=t}catch(e){}

document.addEventListener("click",function(e){var b=e.target.closest&&e.target.closest("[data-toggle-pass]");if(!b)return;var i=document.getElementById(b.dataset.togglePass);if(!i)return;var show=i.type==="password";i.type=show?"text":"password";b.textContent=show?"Hide":"Show"});
