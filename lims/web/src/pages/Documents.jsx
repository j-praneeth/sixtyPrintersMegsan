import React, { useEffect, useState } from 'react';
import { supabase } from '../supabaseClient.js';

const PAGE_SIZE = 200;

function fmtSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleString();
}

export default function Documents() {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [regFilter, setRegFilter] = useState('');
  const [deviceFilter, setDeviceFilter] = useState('');
  const [downloadError, setDownloadError] = useState('');

  useEffect(() => {
    let cancelled = false;
    supabase
      .from('documents')
      .select('*')
      .order('received_at', { ascending: false })
      .limit(PAGE_SIZE)
      .then(({ data }) => {
        if (!cancelled) {
          setDocs(data || []);
          setLoading(false);
        }
      });

    // New prints appear live: realtime INSERT prepends (newest first, no polling).
    const channel = supabase
      .channel('documents-live')
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'documents' },
        (payload) => {
          setDocs((cur) =>
            cur.some((d) => d.id === payload.new.id) ? cur : [payload.new, ...cur]
          );
        }
      )
      .subscribe();
    return () => {
      cancelled = true;
      supabase.removeChannel(channel);
    };
  }, []);

  async function download(doc) {
    setDownloadError('');
    const { data, error } = await supabase.storage
      .from('lims-docs')
      .createSignedUrl(doc.storage_path, 300);
    if (error || !data?.signedUrl) {
      setDownloadError(`Download failed: ${error?.message || 'no URL returned'}`);
      return;
    }
    window.open(data.signedUrl, '_blank', 'noopener');
  }

  const reg = regFilter.trim().toLowerCase();
  const dev = deviceFilter.trim().toLowerCase();
  const shown = docs.filter(
    (d) =>
      (!reg || (d.reg_no || '').toLowerCase().includes(reg)) &&
      (!dev || (d.device_name || '').toLowerCase().includes(dev))
  );

  return (
    <div className="page">
      <section className="card">
        <div className="docs-head">
          <h2>Documents</h2>
          <div className="filters">
            <input
              value={regFilter}
              onChange={(e) => setRegFilter(e.target.value)}
              placeholder="Filter by reg no"
            />
            <input
              value={deviceFilter}
              onChange={(e) => setDeviceFilter(e.target.value)}
              placeholder="Filter by device"
            />
          </div>
        </div>
        {downloadError && <div className="error-note">{downloadError}</div>}
        {loading ? (
          <div className="center-note">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="center-note">No documents{reg || dev ? ' match the filters' : ' yet'}.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Received</th>
                  <th>Reg no</th>
                  <th>Test</th>
                  <th>Device</th>
                  <th>Document</th>
                  <th>Size</th>
                  <th>Printed by</th>
                  <th></th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {shown.map((d) => (
                  <tr key={d.id}>
                    <td className="mono nowrap">{fmtTime(d.received_at)}</td>
                    <td className="mono">{d.reg_no}</td>
                    <td>{d.test_name}</td>
                    <td>
                      <span className="tag">{d.device_name}</span>{' '}
                      <span className="dim">{d.device_type}</span>
                    </td>
                    <td className="doc-name" title={d.docname || ''}>
                      {d.docname}
                    </td>
                    <td className="nowrap">{fmtSize(d.size)}</td>
                    <td>{d.printed_by}</td>
                    <td>
                      {d.encrypted && <span className="badge badge-enc">encrypted</span>}
                      {d.pdf_password && (
                        <button
                          className="btn btn-ghost"
                          title="Show the PDF password"
                          onClick={() =>
                            window.prompt('PDF password (Ctrl+C to copy):', d.pdf_password)
                          }
                        >
                          password
                        </button>
                      )}
                    </td>
                    <td>
                      <button className="btn btn-ghost" onClick={() => download(d)}>
                        Download
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
