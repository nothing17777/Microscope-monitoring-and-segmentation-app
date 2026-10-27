import React from 'react'
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { clock } from './format'

export function token(name, fallback) {
  if (typeof window === 'undefined') return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

// State colours live on the .state-* classes, not :root, so they are looked
// up by name. The backlog line must agree with the headline above it.
const STATE_TOKEN = {
  idle: '--text-muted', 'keeping-up': '--primary', slipping: '--yellow', behind: '--red',
}
export const stateColor = (key) => token(STATE_TOKEN[key] || '--primary', '#1ABB9C')

export function toSeries(samples) {
  return (samples ?? []).map((d) => ({
    t: clock(d.ts),
    backlog: d.backlog,
    segmented: d.segmented,
    found: d.found,
    rate: Number((d.current_rate ?? 0).toFixed(3)),
    cpu: d.cpu_pct,
    ram: d.ram_pct,
    gpu: d.gpu?.gpu_util_pct ?? null,
    gpuTemp: d.gpu?.gpu_temp_c ?? null,
    gpuPower: d.gpu?.gpu_power_w ?? null,
    gpuPowerLimit: d.gpu?.gpu_power_limit_w ?? null,
    vramUsed: d.gpu?.vram_used_mb != null ? Number((d.gpu.vram_used_mb / 1024).toFixed(2)) : null,
    vramTotal: d.gpu?.vram_total_mb != null ? Number((d.gpu.vram_total_mb / 1024).toFixed(1)) : null,
    interMean: d.inter_arrival_mean_s ?? null,
    interP95: d.inter_arrival_p95_s ?? null,
    diskRead: d.disk_read_mbs ?? null,
    diskWrite: d.disk_write_mbs ?? null,
  }))
}

export default function Chart({ data, lines, hero, yLabel, allowDecimals }) {
  const grid = token('--border-color', '#333')
  const muted = token('--text-muted', '#888')
  const surface = token('--bg-surface', '#1a2332')
  // Under 600px the charts become sparklines: no axes, no grid, just shape.
  const spark = typeof window !== 'undefined' && window.innerWidth < 600
  return (
    <div className={hero ? 'chart-box-hero' : 'chart-box'}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}
                   margin={spark ? { top: 4, right: 4, bottom: 0, left: 0 }
                                 : { top: 8, right: 12, bottom: 0, left: -12 }}>
          {spark ? null : <CartesianGrid stroke={grid} strokeDasharray="2 4" vertical={false} />}
          {spark ? null : (
            <XAxis dataKey="t" tick={{ fill: muted, fontSize: 11 }} stroke={grid}
                   tickLine={false} minTickGap={48} />
          )}
          {spark ? null : (
            <YAxis tick={{ fill: muted, fontSize: 11 }} stroke={grid} tickLine={false}
                   width={46} allowDecimals={allowDecimals ?? false}
                   label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft',
                                     fill: muted, fontSize: 11 } : undefined} />
          )}
          <Tooltip contentStyle={{ background: surface, border: `1px solid ${grid}`,
                                   borderRadius: 6, fontSize: 12 }}
                   labelStyle={{ color: muted }} />
          {lines.map((l) => (
            <Line key={l.key} type="monotone" dataKey={l.key} name={l.name}
                  stroke={l.color} strokeWidth={1.75} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
