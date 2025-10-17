export default function FormField({ label, children, error }){
  return (
    <label className="form-field">
      {label && <div className="form-field__label">{label}</div>}
      <div className="form-field__control">{children}</div>
      {error && <div className="form-field__error">{error}</div>}
    </label>
  )
}
