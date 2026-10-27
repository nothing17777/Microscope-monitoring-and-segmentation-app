import React from 'react'

const LEGEND = [
  ['done', 'Segmented', 'well-done'],
  ['segmenting', 'Segmenting', 'well-segmenting'],
  ['acquired', 'Acquired', 'well-acquired'],
  ['failed', 'Failed', 'well-failed'],
  ['empty', 'Not yet', ''],
]

const LABEL = Object.fromEntries(LEGEND.map(([k, l]) => [k, l]))

export default function Plate({ plate }) {
  if (!plate) return null
  const { rows, cols, cells, unplaced } = plate
  const byWell = Object.fromEntries(cells.map((c) => [c.well, c]))
  const rowLetters = Array.from({ length: rows }, (_, r) => String.fromCharCode(65 + r))

  return (
    <div className="plate">
      <div className="plate-grid" style={{ '--cols': cols }}>
        <div className="plate-corner" />
        {Array.from({ length: cols }, (_, c) => (
          <div className="plate-head" key={`h${c}`}>{c + 1}</div>
        ))}
        {rowLetters.map((row) => (
          <React.Fragment key={row}>
            <div className="plate-row-label">{row}</div>
            {Array.from({ length: cols }, (_, c) => {
              const well = `${row}${String(c + 1).padStart(2, '0')}`
              const cell = byWell[well] || { state: 'empty', total: 0, done: 0, pending: 0 }
              return (
                <div
                  key={well}
                  className={`well well-${cell.state}`}
                  // Title carries the numbers: the circle says which state,
                  // hovering says how many, so colour is never the only channel.
                  title={`${well} — ${LABEL[cell.state]}` +
                    (cell.total ? ` · ${cell.done}/${cell.total} segmented` : '')}
                />
              )
            })}
          </React.Fragment>
        ))}
      </div>
      <div className="plate-legend">
        {LEGEND.map(([key, label, cls]) => (
          <span className="key" key={key}>
            <span className={`swatch ${cls}`} /> {label}
          </span>
        ))}
        {unplaced ? (
          <span className="muted">{unplaced} image(s) with no well in their path</span>
        ) : null}
      </div>
    </div>
  )
}
