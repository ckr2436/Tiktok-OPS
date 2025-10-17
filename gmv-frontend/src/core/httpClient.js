import axios from 'axios'
import { apiRoot } from './config.js'

export const http = axios.create({
  baseURL: apiRoot,
  withCredentials: true,
  timeout: 15000,
})

http.interceptors.response.use(
  (res) => res,
  (err) => {
    const payload = err?.response?.data
    const status = err?.response?.status
    const message =
      payload?.error?.message ||
      payload?.detail ||
      err?.message ||
      '网络错误，请稍后再试'
    const e = new Error(message)
    e.status = status
    e.payload = payload
    return Promise.reject(e)
  }
)

export default http

