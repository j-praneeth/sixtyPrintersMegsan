import React, { useEffect, useState } from 'react';
import { supabase } from './supabaseClient.js';
import SignIn from './pages/SignIn.jsx';
import Registrations from './pages/Registrations.jsx';
import Documents from './pages/Documents.jsx';

export default function App() {
  const [session, setSession] = useState(null);
  const [ready, setReady] = useState(false);
  const [page, setPage] = useState('registrations');

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_event, s) => {
      setSession(s);
    });
    return () => sub.subscription.unsubscribe();
  }, []);

  if (!ready) return <div className="center-note">Loading…</div>;
  if (!session) return <SignIn />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" /> LIMS
        </div>
        <nav className="nav">
          <button
            className={page === 'registrations' ? 'nav-btn active' : 'nav-btn'}
            onClick={() => setPage('registrations')}
          >
            Registrations
          </button>
          <button
            className={page === 'documents' ? 'nav-btn active' : 'nav-btn'}
            onClick={() => setPage('documents')}
          >
            Documents
          </button>
        </nav>
        <div className="userbox">
          <span className="user-email">{session.user.email}</span>
          <button className="btn btn-ghost" onClick={() => supabase.auth.signOut()}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">
        {page === 'registrations' ? <Registrations /> : <Documents />}
      </main>
    </div>
  );
}
