import { useEffect } from 'react'

export default function Modal({ open, title, children, onClose, maskClosable=true, escClosable=true, showClose=true }){
  useEffect(() => {
    if (!open || !escClosable) return
    const onKey = (e)=>{ if(e.key === 'Escape') onClose?.() }
    window.addEventListener('keydown', onKey)
    return ()=> window.removeEventListener('keydown', onKey)
  }, [open, escClosable, onClose])

  useEffect(()=>{
    if (!open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return ()=>{ document.body.style.overflow = prev }
  }, [open])

  if(!open) return null
  return (
    <div className="modal-backdrop" onClick={(e)=>{ if (maskClosable && e.target===e.currentTarget) onClose?.() }}>
      <div className="modal" onClick={e=>e.stopPropagation()}>
        <div className="modal__header">
          <div className="modal__title">{title}</div>
          {showClose ? <button className="modal__close" onClick={()=>onClose?.()}>×</button> : <span/>}
        </div>
        <div className="modal__body">{children}</div>
      </div>
    </div>
  )
}

