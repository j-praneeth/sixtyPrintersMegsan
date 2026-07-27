import React, { useCallback, useEffect, useRef, useState } from 'react';
import { supabase } from '../supabaseClient.js';

export default function Registrations() {
  const [deviceTypes, setDeviceTypes] = useState([]);
  const [regs, setRegs] = useState([]);
  const [loading, setLoading] = useState(true);

  // form state
  const [regNo, setRegNo] = useState('');
  const [deviceType, setDeviceType] = useState('');
  const [product, setProduct] = useState('');
  const [tests, setTests] = useState([]);
  const [testInput, setTestInput] = useState('');
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);
  const testInputRef = useRef(null);

  const fetchRegs = useCallback(async () => {
    const { data, error } = await supabase
      .from('registrations')
      .select('id, reg_no, device_type, product, status, updated_at, registration_tests(test_name)')
      .order('updated_at', { ascending: false });
    if (!error) setRegs(data || []);
    setLoading(false);
  }, []);

  useEffect(() => {
    supabase
      .from('device_types')
      .select('id')
      .order('id')
      .then(({ data }) => {
        const types = (data || []).map((r) => r.id);
        setDeviceTypes(types);
        setDeviceType((cur) => cur || types[0] || '');
      });
    fetchRegs();

    // Realtime: any change to registrations or their tests refreshes the list.
    // A refetch (rather than patching state) keeps ordering + joined tests correct.
    const channel = supabase
      .channel('registrations-live')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'registrations' }, fetchRegs)
      .on('postgres_changes', { event: '*', schema: 'public', table: 'registration_tests' }, fetchRegs)
      .subscribe();
    return () => supabase.removeChannel(channel);
  }, [fetchRegs]);

  function addTest(raw) {
    const name = raw.trim();
    if (!name) return;
    setTests((cur) => (cur.includes(name) ? cur : [...cur, name]));
    setTestInput('');
  }

  function onTestKeyDown(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      addTest(testInput);
    } else if (e.key === 'Backspace' && !testInput) {
      setTests((cur) => cur.slice(0, -1));
    }
  }

  async function submit(e) {
    e.preventDefault();
    setFormError('');
    const allTests = testInput.trim() ? [...tests, testInput.trim()] : tests;
    if (!regNo.trim() || !deviceType) {
      setFormError('Registration number and device type are required.');
      return;
    }
    if (allTests.length === 0) {
      setFormError('Add at least one test (type a name and press Enter).');
      return;
    }
    setSaving(true);
    const { data: reg, error } = await supabase
      .from('registrations')
      .insert({ reg_no: regNo.trim(), device_type: deviceType, product: product.trim() })
      .select('id')
      .single();
    if (error) {
      setFormError(error.message);
      setSaving(false);
      return;
    }
    const rows = [...new Set(allTests)].map((t) => ({ registration_id: reg.id, test_name: t }));
    const { error: testErr } = await supabase.from('registration_tests').insert(rows);
    if (testErr) {
      setFormError(`Registration saved but tests failed: ${testErr.message}`);
      setSaving(false);
      return;
    }
    setRegNo('');
    setProduct('');
    setTests([]);
    setTestInput('');
    setSaving(false);
    fetchRegs();
  }

  async function toggleStatus(reg) {
    const next = reg.status === 'open' ? 'closed' : 'open';
    await supabase.from('registrations').update({ status: next }).eq('id', reg.id);
    fetchRegs();
  }

  return (
    <div className="page">
      <section className="card">
        <h2>New registration</h2>
        <form className="reg-form" onSubmit={submit}>
          <div className="form-row">
            <label>
              Registration number
              <input
                value={regNo}
                onChange={(e) => setRegNo(e.target.value)}
                placeholder="R-2026-0001"
                required
              />
            </label>
            <label>
              Device type
              <select value={deviceType} onChange={(e) => setDeviceType(e.target.value)}>
                {deviceTypes.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label className="grow">
              Product / description
              <input
                value={product}
                onChange={(e) => setProduct(e.target.value)}
                placeholder="Sample description"
              />
            </label>
          </div>
          <label>
            Tests <span className="hint">(type a name, press Enter to add)</span>
            <div className="chip-box" onClick={() => testInputRef.current?.focus()}>
              {tests.map((t) => (
                <span className="chip" key={t}>
                  {t}
                  <button
                    type="button"
                    className="chip-x"
                    aria-label={`Remove ${t}`}
                    onClick={() => setTests((cur) => cur.filter((x) => x !== t))}
                  >
                    &times;
                  </button>
                </span>
              ))}
              <input
                ref={testInputRef}
                className="chip-input"
                value={testInput}
                onChange={(e) => setTestInput(e.target.value)}
                onKeyDown={onTestKeyDown}
                placeholder={tests.length ? '' : 'e.g. rx-test'}
              />
            </div>
          </label>
          {formError && <div className="error-note">{formError}</div>}
          <div>
            <button className="btn btn-primary" type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Create registration'}
            </button>
          </div>
        </form>
      </section>

      <section className="card">
        <h2>Registrations</h2>
        {loading ? (
          <div className="center-note">Loading…</div>
        ) : regs.length === 0 ? (
          <div className="center-note">No registrations yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Reg no</th>
                  <th>Device type</th>
                  <th>Product</th>
                  <th>Tests</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {regs.map((r) => (
                  <tr key={r.id} className={r.status === 'closed' ? 'row-closed' : ''}>
                    <td className="mono">{r.reg_no}</td>
                    <td>
                      <span className="tag">{r.device_type}</span>
                    </td>
                    <td>{r.product}</td>
                    <td>
                      <div className="chip-list">
                        {(r.registration_tests || []).map((t) => (
                          <span className="chip chip-sm" key={t.test_name}>
                            {t.test_name}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td>
                      <span className={r.status === 'open' ? 'badge badge-open' : 'badge badge-closed'}>
                        {r.status}
                      </span>
                    </td>
                    <td>
                      <button className="btn btn-ghost" onClick={() => toggleStatus(r)}>
                        {r.status === 'open' ? 'Close' : 'Reopen'}
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
