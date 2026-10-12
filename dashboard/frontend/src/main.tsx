import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { AccountProvider } from './state/AccountContext';
import './styles.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      // Live values come from bounded polling; failed polls keep the
      // last-known data visible rather than flipping to errors.
      refetchIntervalInBackground: false,
    },
  },
});

const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('Missing #root element');
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AccountProvider>
        <App />
      </AccountProvider>
    </QueryClientProvider>
  </StrictMode>,
);
