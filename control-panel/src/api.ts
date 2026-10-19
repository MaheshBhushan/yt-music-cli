export type Track = {
  video_id: string;
  title: string;
  artist: string;
  album: string;
  duration: string;
  duration_seconds: number;
  thumbnail?: string;
};

export type Snapshot = {
  status: {
    current: Track | null;
    index: number;
    count: number;
    paused: boolean;
    volume: number;
    position: number;
  };
  queue: { tracks: (Track & { entry_id: number })[]; index: number };
};

type Commands = {
  search: { query: string };
  play: Track;
  enqueue: Track;
  enqueue_next: Track;
  toggle: Record<string, never>;
  next: Record<string, never>;
  prev: Record<string, never>;
  seek: { seconds: number };
  volume: { level: number };
  queue_clear: Record<string, never>;
  queue_play: { index: number };
  queue_remove: { index: number };
  queue_move: { from_index: number; to_index: number };
};

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...options, signal: AbortSignal.timeout(35000) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Player request failed (${response.status})`);
  return data as T;
}

export function command<C extends keyof Commands>(cmd: C, args: Commands[C]) {
  return request<{ tracks?: Track[] }>('/api/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cmd, args }),
  });
}

export const snapshot = () => request<Snapshot>('/api/status');
