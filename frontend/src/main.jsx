import React from 'react'
import { createRoot } from 'react-dom/client'
// AdminLTE 4 ships Bootstrap 5.3 bundled into adminlte.css, so this is the
// only stylesheet needed. The ESM build drives the sidebar toggle.
import 'admin-lte/dist/css/adminlte.min.css'
import 'admin-lte/dist/js/adminlte.esm.min.js'
import App from './App.jsx'
import './styles.css'

createRoot(document.getElementById('root')).render(<App />)
