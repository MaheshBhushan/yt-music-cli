import { useEffect, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { command, snapshot, type Snapshot, type Track } from './api';
import './style.css';

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
const message = (error: unknown) => error instanceof Error ? error.message : 'Could not reach the local player';

function App() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Track[]>([]);
  const [searched, setSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [connectionError, setConnectionError] = useState('');

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const next = await snapshot();
        if (active) { setData(next); setConnectionError(''); }
      } catch (err) {
        if (active) setConnectionError(message(err));
      } finally {
        if (active) timer = setTimeout(refresh, 1000);
      }
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      setData(await snapshot());
      setConnectionError('');
    } catch (err) {
      setError(message(err));
    } finally { setBusy(false); }
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setError('');
    try {
      const response = await command('search', { query: query.trim() });
      setResults(response.tracks ?? []);
      setSearched(true);
    } catch (err) { setError(message(err)); }
    finally { setSearching(false); }
  }

  const status = data?.status;
  const current = status?.current;
  const disabled = busy || !data || !!connectionError;

  return <main>
    <header><div className="brand">ytm<span>LOCAL PLAYER</span></div>
      <span className="connection">{connectionError ? 'Disconnected' : data ? 'Connected to Python player' : 'Connecting…'}</span></header>
    <section className="player" aria-labelledby="now-title">
      <p className="eyebrow">NOW PLAYING</p>
      <h1 id="now-title">{current?.title || 'Your next favourite is waiting'}</h1>
      <p className="artist">{current ? current.artist : 'Search the catalogue and start listening.'}</p>
      <div className="timeline"><span>{clock(status?.position ?? 0)}</span>
        <progress aria-label="Track progress" value={status?.position ?? 0} max={Math.max(current?.duration_seconds ?? 0, status?.position ?? 0, 1)} />
        <span>{clock(current?.duration_seconds ?? 0)}</span></div>
      <fieldset disabled={disabled || !current} className="transport" aria-label="Playback controls">
        <button onClick={() => void act(() => command('prev', {}))}>Previous</button>
        <button onClick={() => void act(() => command('seek', { seconds: -10 }))}>−10 s</button>
        <button className="primary" onClick={() => void act(() => command('toggle', {}))}>{status?.paused ? 'Resume' : 'Pause'}</button>
        <button onClick={() => void act(() => command('seek', { seconds: 10 }))}>+10 s</button>
        <button onClick={() => void act(() => command('next', {}))}>Next</button>
      </fieldset>
      <label className="volume">Volume
        <input aria-label="Volume" type="range" min="0" max="100" step="5" value={status?.volume ?? 70} disabled={disabled}
          onChange={event => void act(() => command('volume', { level: Number(event.target.value) }))} />
        <output>{Math.round(status?.volume ?? 70)}%</output></label>
      <p className="play-state" role="status">{busy ? 'Updating player…' : current ? status?.paused ? 'Paused' : 'Playing' : 'Player is idle'}</p>
    </section>
    {(error || connectionError) && <div className="error" role="alert">{error || connectionError}<button onClick={() => window.location.reload()}>Reconnect</button></div>}
    <div className="columns">
      <section aria-labelledby="search-title">
        <div className="section-heading"><h2 id="search-title">Discover</h2><span>YOUTUBE MUSIC</span></div>
        <form onSubmit={event => void search(event)} role="search">
          <label className="sr-only" htmlFor="query">Search songs or artists</label>
          <input id="query" type="search" placeholder="Search songs or artists" value={query} maxLength={200} onChange={event => setQuery(event.target.value)} />
          <button className="primary" disabled={searching || !query.trim()}>{searching ? 'Searching…' : 'Search'}</button>
        </form>
        <p className="hint" role="status">{searching ? 'Searching the catalogue…' : searched ? `${results.length} results` : 'Find a song. Build your queue.'}</p>
        <ul className="tracks" aria-label="Search results">{results.map(track => <li key={track.video_id}>
          <div className="track-info"><strong>{track.title}</strong><span>{track.artist} · {track.duration}</span></div>
          <div className="row-actions">
            <button disabled={disabled} aria-label={`Play ${track.title}`} onClick={() => void act(() => command('play', track))}>Play</button>
            <button disabled={disabled} aria-label={`Queue ${track.title}`} onClick={() => void act(() => command('enqueue', track))}>+ Queue</button>
            <button disabled={disabled} aria-label={`Play next ${track.title}`} onClick={() => void act(() => command('enqueue_next', track))}>Up next</button>
          </div></li>)}</ul>
        {searched && !searching && results.length === 0 && <p className="empty">No songs found. Try another title or artist.</p>}
      </section>
      <section aria-labelledby="queue-title">
        <div className="section-heading"><h2 id="queue-title">Queue <span>{data?.queue.tracks.length ?? 0}</span></h2>
          <button disabled={disabled || !data?.queue.tracks.length} onClick={() => void act(() => command('queue_clear', {}))}>Clear upcoming</button></div>
        <p className="hint">Your listening order. The current song stays when cleared.</p>
        <ol className="tracks queue" aria-label="Playback queue">{data?.queue.tracks.map((track, index) => <li key={track.entry_id} aria-current={index === data.queue.index ? 'true' : undefined}>
          <span className="track-number">{index === data.queue.index ? '▶' : index + 1}</span>
          <div className="track-info"><strong>{track.title}</strong><span>{track.artist} · {track.duration}</span></div>
          <div className="row-actions">
            <button disabled={disabled} aria-label={`Jump to ${track.title}`} onClick={() => void act(() => command('queue_play', { index }))}>Play</button>
            <button disabled={disabled || index === 0} aria-label={`Move up ${track.title}`} onClick={() => void act(() => command('queue_move', { from_index: index, to_index: index - 1 }))}>↑</button>
            <button disabled={disabled || index === data.queue.tracks.length - 1} aria-label={`Move down ${track.title}`} onClick={() => void act(() => command('queue_move', { from_index: index, to_index: index + 1 }))}>↓</button>
            <button disabled={disabled} aria-label={`Remove ${track.title}`} onClick={() => void act(() => command('queue_remove', { index }))}>×</button>
          </div></li>)}</ol>
        {!data?.queue.tracks.length && <p className="empty">Your queue is empty. Add a song from Discover.</p>}
      </section>
    </div>
    <footer>Audio plays on this computer through mpv. Stop the Python server to end this player session.</footer>
  </main>;
}

createRoot(document.getElementById('root')!).render(<App />);
