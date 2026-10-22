import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { api } from './api';
import App from './App';
import './index.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root missing from index.html');

createRoot(root).render(
  <StrictMode>
    <App api={api} />
  </StrictMode>,
);
