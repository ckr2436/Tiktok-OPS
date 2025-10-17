// src/utils/booleans.js
export function parseBoolLike(value){
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string'){
    const normalized = value.trim().toLowerCase()
    if (!normalized) return false
    if (['true','1','yes','y','on'].includes(normalized)) return true
    if (['false','0','no','n','off','none','null','undefined'].includes(normalized)) return false
  }
  return Boolean(value)
}

export default parseBoolLike
