// public/doc.js - 轻量 Markdown 渲染 + TOC（CSP 友好，无第三方）
(function () {
  const $ = (s, r = document) => r.querySelector(s);

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function inlineMd(t) {
    t = t.replace(/\[([^\]]+?)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    t = t.replace(/`([^`]+?)`/g,'<code class="i">$1</code>');
    t = t.replace(/\*\*([^*]+?)\*\*/g,'<strong>$1</strong>');
    t = t.replace(/\*([^*]+?)\*/g,'<em>$1</em>');
    return t;
  }
  function md2html(md) {
    const lines = md.replace(/\r\n/g,"\n").split("\n");
    let html = "", inUl=false, inOl=false, inP=false;

    const close = () => {
      if (inP){ html += "</p>"; inP=false; }
      if (inUl){ html += "</ul>"; inUl=false; }
      if (inOl){ html += "</ol>"; inOl=false; }
    };

    for (const raw of lines) {
      const line = raw.trimEnd();

      const h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        close();
        const n = h[1].length, txt = inlineMd(escapeHtml(h[2].trim()));
        html += `<h${n}>${txt}</h${n}>`;
        continue;
      }

      const ol = /^\d+\.\s+(.+)$/.exec(line);
      if (ol) {
        if (inP){ html += "</p>"; inP=false; }
        if (inUl){ html += "</ul>"; inUl=false; }
        if (!inOl){ html += "<ol>"; inOl=true; }
        html += `<li>${inlineMd(escapeHtml(ol[1]))}</li>`;
        continue;
      }

      const ul = /^[-*]\s+(.+)$/.exec(line);
      if (ul) {
        if (inP){ html += "</p>"; inP=false; }
        if (inOl){ html += "</ol>"; inOl=false; }
        if (!inUl){ html += "<ul>"; inUl=true; }
        html += `<li>${inlineMd(escapeHtml(ul[1]))}</li>`;
        continue;
      }

      if (!line.trim()) { close(); continue; }

      if (!inP){ close(); html += "<p>"; inP=true; }
      html += inlineMd(escapeHtml(line)) + " ";
    }
    close();
    return html;
  }

  function slugify(s){
    return s.toLowerCase()
      .replace(/[\s]+/g,'-')
      .replace(/[^\w\-一-龥]/g,'')
      .replace(/\-+/g,'-')
      .replace(/^\-|\-$/g,'');
  }

  function buildToc(root, tocEl){
    const hs = root.querySelectorAll('h2, h3');
    if (!hs.length) return;

    const ul = document.createElement('ul');
    ul.className = 'toc-list';

    hs.forEach(h => {
      if (!h.id){
        h.id = slugify(h.textContent || 'h');
      }
      const li = document.createElement('li');
      li.className = 'toc-item ' + h.tagName.toLowerCase();
      const a = document.createElement('a');
      a.href = '#' + h.id;
      a.textContent = h.textContent;
      li.appendChild(a);
      ul.appendChild(li);
    });

    tocEl.innerHTML = '';
    tocEl.appendChild(ul);
    tocEl.hidden = false;

    // 高亮当前
    const io = new IntersectionObserver(entries=>{
      const visible = entries.filter(e=>e.isIntersecting).sort((a,b)=>b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      const id = visible.target.id;
      tocEl.querySelectorAll('a').forEach(a=>{
        a.classList.toggle('active', a.getAttribute('href') === '#' + id);
      });
    }, { rootMargin: '0px 0px -60% 0px', threshold: 0 });
    hs.forEach(h => io.observe(h));
  }

  async function run(){
    const docEl = $('#doc');
    const tocEl = $('#toc');
    const back = $('#btn-back');
    const mdUrl = (document.currentScript && document.currentScript.dataset.md) || './terms.md';

    try{
      const res = await fetch(mdUrl, { cache: 'no-store' });
      if(!res.ok) throw new Error('HTTP '+res.status);
      const md  = await res.text();
      docEl.innerHTML = md2html(md);
      buildToc(docEl, tocEl);
    }catch(e){
      docEl.textContent = '加载失败';
      console.error(e);
    }

    back?.addEventListener('click', (e)=>{
      e.preventDefault();
      if (history.length > 1) history.back();
      else location.assign('/login');
    });
  }

  run();
})();

