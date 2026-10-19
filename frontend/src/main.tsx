import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './app/App'
import AccessGate from './features/access/AccessGate'
import './app/styles.css'
import './features/library/persistence.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode><AccessGate>{readOnly => <App readOnly={readOnly} />}</AccessGate></StrictMode>,
)
