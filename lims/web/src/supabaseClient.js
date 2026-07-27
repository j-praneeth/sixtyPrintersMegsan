import { createClient } from '@supabase/supabase-js';

// Anon key + RLS only. The service role key never appears in web code
// (it lives on the central desktop, in the hub's settings).
const url = import.meta.env.VITE_SUPABASE_URL;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  // Surfaced in the console instead of a blank white page.
  console.error(
    'Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY — copy .env.example to .env and fill it in.'
  );
}

export const supabase = createClient(url || 'http://localhost', anonKey || 'missing');
