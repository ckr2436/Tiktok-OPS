import React from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store } from './app/store.js'
import router from './routes/index.jsx'
import './styles/globals.css'
import HealthGate from './core/HealthGate.jsx'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

const root = createRoot(document.getElementById('root'))
root.render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <HealthGate>
          <RouterProvider router={router} />
        </HealthGate>
      </Provider>
    </QueryClientProvider>
  </React.StrictMode>,
)

