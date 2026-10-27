import React from 'react'
import { createRoot } from 'react-dom/client'
// Gentelella v4 is the whole design system — tokens, layout and components,
// with no Bootstrap and no jQuery. theme.scss pulls in the partials used here.
import './theme.scss'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(<App />)
