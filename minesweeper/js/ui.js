const NUMBER_CLASSES = ['num-1', 'num-2', 'num-3', 'num-4', 'num-5', 'num-6', 'num-7', 'num-8'];

export function formatCounter(value) {
  const clamped = Math.max(-99, Math.min(999, value));
  const sign = clamped < 0 ? '-' : '';
  return sign + String(Math.abs(clamped)).padStart(3, '0');
}

export function computeCellSize(cols) {
  if (cols <= 9) return 36;
  if (cols <= 16) return 28;
  if (cols <= 24) return 22;
  return 18;
}

export class UI {
  constructor(elements) {
    this.boardEl = elements.boardEl;
    this.mineCounterEl = elements.mineCounterEl;
    this.timerEl = elements.timerEl;
    this.faceBtn = elements.faceBtn;
    this.onLeftClick = null;
    this.onRightClick = null;
    this.onChord = null;
    this.mouseButtons = 0;
  }

  setCallbacks({ onLeftClick, onRightClick, onChord }) {
    this.onLeftClick = onLeftClick;
    this.onRightClick = onRightClick;
    this.onChord = onChord;
  }

  updateMineCounter(value) {
    this.mineCounterEl.textContent = formatCounter(value);
  }

  updateTimer(seconds) {
    this.timerEl.textContent = formatCounter(Math.min(999, seconds));
  }

  updateFace(status) {
    const faces = {
      idle: '🙂',
      playing: '😐',
      won: '😎',
      lost: '💀',
    };
    this.faceBtn.textContent = faces[status] || '🙂';
  }

  renderBoard(game) {
    const { rows, cols, board, status } = game;
    const cellSize = computeCellSize(cols);
    this.boardEl.style.setProperty('--cell-size', `${cellSize}px`);
    this.boardEl.style.gridTemplateColumns = `repeat(${cols}, var(--cell-size))`;
    this.boardEl.innerHTML = '';

    const disabled = status === 'won' || status === 'lost';

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const cell = board[r][c];
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cell';
        btn.dataset.row = String(r);
        btn.dataset.col = String(c);
        btn.setAttribute('role', 'gridcell');
        btn.disabled = disabled;

        this.applyCellState(btn, cell, game);

        btn.addEventListener('click', (e) => {
          if (e.button !== 0) return;
          if (this.onLeftClick) this.onLeftClick(r, c);
        });

        btn.addEventListener('dblclick', (e) => {
          e.preventDefault();
          if (disabled) return;
          if (this.onChord) this.onChord(r, c);
        });

        btn.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          if (this.onRightClick) this.onRightClick(r, c);
        });

        btn.addEventListener('mousedown', (e) => {
          if (disabled) return;
          this.mouseButtons |= 1 << e.button;

          const isChord =
            (this.mouseButtons & 1 && this.mouseButtons & 2) ||
            e.button === 1;

          if (isChord && this.onChord) {
            e.preventDefault();
            this.onChord(r, c);
          }
        });

        btn.addEventListener('mouseup', () => {
          this.mouseButtons = 0;
        });

        btn.addEventListener('mouseleave', () => {
          this.mouseButtons = 0;
        });

        this.boardEl.appendChild(btn);
      }
    }
  }

  applyCellState(btn, cell, game) {
    btn.className = 'cell';
    btn.textContent = '';
    NUMBER_CLASSES.forEach((cls) => btn.classList.remove(cls));

    const [triggerR, triggerC] = game.triggeredMine || [null, null];
    const isTriggered =
      triggerR !== null &&
      Number(btn.dataset.row) === triggerR &&
      Number(btn.dataset.col) === triggerC;

    if (cell.isRevealed) {
      btn.classList.add('revealed');
      if (cell.isMine) {
        btn.classList.add('mine');
        btn.textContent = '💣';
        if (isTriggered) btn.classList.add('triggered');
      } else if (cell.neighborMines > 0) {
        btn.textContent = String(cell.neighborMines);
        btn.classList.add(`num-${cell.neighborMines}`);
      }
    } else if (cell.isFlagged) {
      btn.classList.add('hidden-cell', 'flagged');
      btn.textContent = '🚩';
    } else {
      btn.classList.add('hidden-cell');
    }

    if (game.status === 'lost' && cell.isMine && !cell.isFlagged && !cell.isRevealed) {
      btn.classList.remove('hidden-cell');
      btn.classList.add('revealed', 'mine');
      btn.textContent = '💣';
    }
  }

  refresh(game) {
    const { rows, cols, board, status } = game;
    const disabled = status === 'won' || status === 'lost';
    const cells = this.boardEl.querySelectorAll('.cell');

    cells.forEach((btn) => {
      const r = Number(btn.dataset.row);
      const c = Number(btn.dataset.col);
      btn.disabled = disabled;
      this.applyCellState(btn, board[r][c], game);
    });

    if (cells.length !== rows * cols) {
      this.renderBoard(game);
    }
  }
}
