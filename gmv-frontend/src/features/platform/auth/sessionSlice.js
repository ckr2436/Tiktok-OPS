import { createSlice } from '@reduxjs/toolkit'

const initialState = {
  data: null,
  checked: false,
}

const slice = createSlice({
  name: 'session',
  initialState,
  reducers: {
    setSession(state, action){ state.data = action.payload; state.checked = true },
    clearSession(state){ state.data = null; state.checked = true },
    markChecked(state){ state.checked = true },
  }
})

export const { setSession, clearSession, markChecked } = slice.actions
export default slice.reducer
