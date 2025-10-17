// src/components/ui/Doc.jsx
import { marked } from 'marked'
import terms from '../../pages/terms.md?raw'
import privacy from '../../pages/privacy.md?raw'

// 轻配置：支持 GFM、自动换行
marked.setOptions({ gfm: true, breaks: true })

export default function Doc({ kind }){
  const md = kind === 'terms' ? terms : privacy
  const html = marked.parse(md || '')
  return <article className="doc-body" dangerouslySetInnerHTML={{ __html: html }} />
}

