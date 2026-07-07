/**
 * Minesweeper core game logic
 */

export const DIFFICULTIES = {
  beginner: { rows: 9, cols: 9, mines: 10, label: '初级' },
  intermediate: { rows: 16, cols: 16, mines: 40, label: '中级' },
  expert: { rows: 16, cols: 30, mines: 99, label: '高级' },
};

export const CUSTOM_LIMITS = {
  colsMin: 9,
  colsMax: 30,
  rowsMin: 9,
  rowsMax: 24,
};

export function createCell() {
  return {
    isMine: false,
    isRevealed: false,
    isFlagged: false,
    neighborMines: 0,
  };
}

export function createBoard(rows, cols) {
  return Array.from({ length: rows }, () =>
    Array.from({ length: cols }, () => createCell())
  );
}

export function validateCustomDifficulty(cols, rows, mines) {
  const { colsMin, colsMax, rowsMin, rowsMax } = CUSTOM_LIMITS;
  if (!Number.isInteger(cols) || cols < colsMin || cols > colsMax) {
    return `宽度须在 ${colsMin} ~ ${colsMax} 之间`;
  }
  if (!Number.isInteger(rows) || rows < rowsMin || rows > rowsMax) {
    return `高度须在 ${rowsMin} ~ ${rowsMax} 之间`;
  }
  const maxMines = cols * rows - 9;
  if (!Number.isInteger(mines) || mines < 1 || mines > maxMines) {
    return `雷数须在 1 ~ ${maxMines} 之间`;
  }
  return null;
}

function inBounds(rows, cols, r, c) {
  return r >= 0 && r < rows && c >= 0 && c < cols;
}

function forEachNeighbor(rows, cols, r, c, fn) {
  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      if (dr === 0 && dc === 0) continue;
      const nr = r + dr;
      const nc = c + dc;
      if (inBounds(rows, cols, nr, nc)) fn(nr, nc);
    }
  }
}

function isInSafeZone(rows, cols, r, c, safeRow, safeCol) {
  return Math.abs(r - safeRow) <= 1 && Math.abs(c - safeCol) <= 1;
}

function computeNeighborMines(board, rows, cols) {
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (board[r][c].isMine) {
        board[r][c].neighborMines = 0;
        continue;
      }
      let count = 0;
      forEachNeighbor(rows, cols, r, c, (nr, nc) => {
        if (board[nr][nc].isMine) count++;
      });
      board[r][c].neighborMines = count;
    }
  }
}

function placeMines(board, rows, cols, mineCount, safeRow, safeCol) {
  const candidates = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (!isInSafeZone(rows, cols, r, c, safeRow, safeCol)) {
        candidates.push([r, c]);
      }
    }
  }

  if (mineCount > candidates.length) {
    throw new Error('雷数超过可放置区域');
  }

  for (let i = candidates.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [candidates[i], candidates[j]] = [candidates[j], candidates[i]];
  }

  for (let i = 0; i < mineCount; i++) {
    const [r, c] = candidates[i];
    board[r][c].isMine = true;
  }

  computeNeighborMines(board, rows, cols);
}

export class Game {
  constructor(rows, cols, mineCount) {
    this.rows = rows;
    this.cols = cols;
    this.mineCount = mineCount;
    this.board = createBoard(rows, cols);
    this.status = 'idle';
    this.flagsPlaced = 0;
    this.firstClickDone = false;
    this.triggeredMine = null;
  }

  reset(rows, cols, mineCount) {
    this.rows = rows;
    this.cols = cols;
    this.mineCount = mineCount;
    this.board = createBoard(rows, cols);
    this.status = 'idle';
    this.flagsPlaced = 0;
    this.firstClickDone = false;
    this.triggeredMine = null;
  }

  canInteract() {
    return this.status === 'idle' || this.status === 'playing';
  }

  countNeighborFlags(r, c) {
    let count = 0;
    forEachNeighbor(this.rows, this.cols, r, c, (nr, nc) => {
      if (this.board[nr][nc].isFlagged) count++;
    });
    return count;
  }

  checkWin() {
    for (let r = 0; r < this.rows; r++) {
      for (let c = 0; c < this.cols; c++) {
        const cell = this.board[r][c];
        if (!cell.isMine && !cell.isRevealed) return false;
      }
    }
    this.status = 'won';
    return true;
  }

  revealAllMines(triggerR, triggerC) {
    this.triggeredMine = [triggerR, triggerC];
    for (let r = 0; r < this.rows; r++) {
      for (let c = 0; c < this.cols; c++) {
        const cell = this.board[r][c];
        if (cell.isMine) {
          cell.isRevealed = true;
        }
      }
    }
  }

  revealCell(r, c) {
    if (!this.canInteract()) return { changed: false };
    if (!inBounds(this.rows, this.cols, r, c)) return { changed: false };

    const cell = this.board[r][c];
    if (cell.isRevealed || cell.isFlagged) return { changed: false };

    if (!this.firstClickDone) {
      placeMines(this.board, this.rows, this.cols, this.mineCount, r, c);
      this.firstClickDone = true;
      this.status = 'playing';
    }

    if (cell.isMine) {
      this.status = 'lost';
      this.revealAllMines(r, c);
      return { changed: true, hitMine: true };
    }

    const revealed = [];
    const queue = [[r, c]];
    const visited = new Set([`${r},${c}`]);

    while (queue.length > 0) {
      const [cr, cc] = queue.shift();
      const current = this.board[cr][cc];
      if (current.isRevealed || current.isFlagged) continue;

      current.isRevealed = true;
      revealed.push([cr, cc]);

      if (current.neighborMines === 0) {
        forEachNeighbor(this.rows, this.cols, cr, cc, (nr, nc) => {
          const key = `${nr},${nc}`;
          if (visited.has(key)) return;
          const neighbor = this.board[nr][nc];
          if (!neighbor.isMine && !neighbor.isRevealed && !neighbor.isFlagged) {
            visited.add(key);
            queue.push([nr, nc]);
          }
        });
      }
    }

    const won = this.checkWin();
    return { changed: revealed.length > 0, revealed, won };
  }

  toggleFlag(r, c) {
    if (!this.canInteract()) return { changed: false };
    if (!inBounds(this.rows, this.cols, r, c)) return { changed: false };

    const cell = this.board[r][c];
    if (cell.isRevealed) return { changed: false };

    if (cell.isFlagged) {
      cell.isFlagged = false;
      this.flagsPlaced--;
    } else {
      cell.isFlagged = true;
      this.flagsPlaced++;
    }

    return { changed: true, flagged: cell.isFlagged };
  }

  chordReveal(r, c) {
    if (this.status !== 'playing') return { changed: false };
    if (!inBounds(this.rows, this.cols, r, c)) return { changed: false };

    const cell = this.board[r][c];
    if (!cell.isRevealed || cell.neighborMines === 0) return { changed: false };

    const flagCount = this.countNeighborFlags(r, c);
    if (flagCount !== cell.neighborMines) return { changed: false };

    const toReveal = [];
    forEachNeighbor(this.rows, this.cols, r, c, (nr, nc) => {
      const neighbor = this.board[nr][nc];
      if (!neighbor.isRevealed && !neighbor.isFlagged) {
        toReveal.push([nr, nc]);
      }
    });

    if (toReveal.length === 0) return { changed: false };

    let anyChanged = false;
    let hitMine = false;
    let won = false;

    for (const [nr, nc] of toReveal) {
      const result = this.revealCell(nr, nc);
      if (result.changed) anyChanged = true;
      if (result.hitMine) hitMine = true;
      if (result.won) won = true;
      if (hitMine) break;
    }

    return { changed: anyChanged, hitMine, won };
  }

  remainingMines() {
    return this.mineCount - this.flagsPlaced;
  }
}
