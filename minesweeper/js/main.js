import { Game, DIFFICULTIES, validateCustomDifficulty } from './game.js';
import { UI } from './ui.js';

class MinesweeperApp {
  constructor() {
    this.currentDifficulty = 'beginner';
    this.customConfig = { rows: 16, cols: 16, mines: 40 };
    this.timerInterval = null;
    this.elapsedSeconds = 0;

    this.ui = new UI({
      boardEl: document.getElementById('board'),
      mineCounterEl: document.getElementById('mine-counter'),
      timerEl: document.getElementById('timer'),
      faceBtn: document.getElementById('face-btn'),
    });

    const config = DIFFICULTIES.beginner;
    this.game = new Game(config.rows, config.cols, config.mines);

    this.bindEvents();
    this.ui.setCallbacks({
      onLeftClick: (r, c) => this.handleLeftClick(r, c),
      onRightClick: (r, c) => this.handleRightClick(r, c),
      onChord: (r, c) => this.handleChord(r, c),
    });

    this.startNewGame();
  }

  bindEvents() {
    document.querySelectorAll('.difficulty-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const difficulty = btn.dataset.difficulty;
        this.selectDifficulty(difficulty);
      });
    });

    document.getElementById('new-game-btn').addEventListener('click', () => {
      this.startNewGame();
    });

    document.getElementById('face-btn').addEventListener('click', () => {
      this.startNewGame();
    });

    document.getElementById('custom-apply-btn').addEventListener('click', () => {
      this.applyCustomDifficulty();
    });
  }

  selectDifficulty(difficulty) {
    this.currentDifficulty = difficulty;

    document.querySelectorAll('.difficulty-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.difficulty === difficulty);
    });

    const customPanel = document.getElementById('custom-panel');
    customPanel.classList.toggle('hidden', difficulty !== 'custom');

    if (difficulty !== 'custom') {
      this.startNewGame();
    }
  }

  applyCustomDifficulty() {
    const cols = parseInt(document.getElementById('custom-cols').value, 10);
    const rows = parseInt(document.getElementById('custom-rows').value, 10);
    const mines = parseInt(document.getElementById('custom-mines').value, 10);
    const errorEl = document.getElementById('custom-error');

    const error = validateCustomDifficulty(cols, rows, mines);
    if (error) {
      errorEl.textContent = error;
      errorEl.classList.remove('hidden');
      return;
    }

    errorEl.classList.add('hidden');
    this.customConfig = { rows, cols, mines };
    this.startNewGame();
  }

  getCurrentConfig() {
    if (this.currentDifficulty === 'custom') {
      return this.customConfig;
    }
    return DIFFICULTIES[this.currentDifficulty];
  }

  startNewGame() {
    this.stopTimer();
    this.elapsedSeconds = 0;

    if (this.currentDifficulty === 'custom') {
      const cols = parseInt(document.getElementById('custom-cols').value, 10);
      const rows = parseInt(document.getElementById('custom-rows').value, 10);
      const mines = parseInt(document.getElementById('custom-mines').value, 10);
      const errorEl = document.getElementById('custom-error');
      const error = validateCustomDifficulty(cols, rows, mines);

      if (error) {
        errorEl.textContent = error;
        errorEl.classList.remove('hidden');
        return;
      }

      errorEl.classList.add('hidden');
      this.customConfig = { rows, cols, mines };
    }

    const config = this.getCurrentConfig();
    this.game.reset(config.rows, config.cols, config.mines);

    this.ui.updateTimer(0);
    this.ui.updateMineCounter(this.game.remainingMines());
    this.ui.updateFace('idle');
    this.ui.renderBoard(this.game);
  }

  startTimer() {
    if (this.timerInterval) return;
    this.timerInterval = setInterval(() => {
      this.elapsedSeconds++;
      this.ui.updateTimer(this.elapsedSeconds);
      if (this.elapsedSeconds >= 999) {
        this.stopTimer();
      }
    }, 1000);
  }

  stopTimer() {
    if (this.timerInterval) {
      clearInterval(this.timerInterval);
      this.timerInterval = null;
    }
  }

  handleLeftClick(r, c) {
    if (!this.game.canInteract()) return;

    const wasIdle = this.game.status === 'idle';
    const result = this.game.revealCell(r, c);

    if (!result.changed) return;

    if (wasIdle) {
      this.startTimer();
      this.ui.updateFace('playing');
    }

    this.ui.refresh(this.game);
    this.ui.updateMineCounter(this.game.remainingMines());

    if (result.hitMine) {
      this.stopTimer();
      this.ui.updateFace('lost');
      this.ui.renderBoard(this.game);
    } else if (result.won || this.game.status === 'won') {
      this.stopTimer();
      this.ui.updateFace('won');
      this.ui.renderBoard(this.game);
    }
  }

  handleRightClick(r, c) {
    if (!this.game.canInteract()) return;

    const result = this.game.toggleFlag(r, c);
    if (!result.changed) return;

    this.ui.refresh(this.game);
    this.ui.updateMineCounter(this.game.remainingMines());
  }

  handleChord(r, c) {
    if (this.game.status !== 'playing') return;

    const result = this.game.chordReveal(r, c);
    if (!result.changed) return;

    this.ui.refresh(this.game);
    this.ui.updateMineCounter(this.game.remainingMines());

    if (result.hitMine) {
      this.stopTimer();
      this.ui.updateFace('lost');
      this.ui.renderBoard(this.game);
    } else if (result.won || this.game.status === 'won') {
      this.stopTimer();
      this.ui.updateFace('won');
      this.ui.renderBoard(this.game);
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  new MinesweeperApp();
});
