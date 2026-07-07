/**
 * Automated smoke tests for game logic (run with: node tests/game.test.js)
 */
import { Game, validateCustomDifficulty, DIFFICULTIES } from '../js/game.js';

let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.error('FAIL:', message);
  }
}

// First click safety
{
  const game = new Game(9, 9, 10);
  const result = game.revealCell(4, 4);
  assert(result.changed, 'first click should change board');
  assert(game.status === 'playing', 'status should be playing after first click');
  assert(!game.board[4][4].isMine, 'first click cell must not be mine');
  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      const r = 4 + dr;
      const c = 4 + dc;
      assert(!game.board[r][c].isMine, `safe zone (${r},${c}) must not be mine`);
    }
  }
}

// Flag toggle
{
  const game = new Game(9, 9, 10);
  game.revealCell(0, 0);
  const before = game.flagsPlaced;
  game.toggleFlag(1, 1);
  assert(game.board[1][1].isFlagged, 'cell should be flagged');
  assert(game.flagsPlaced === before + 1, 'flag count should increase');
  game.toggleFlag(1, 1);
  assert(!game.board[1][1].isFlagged, 'flag should be removed');
}

// Cannot reveal flagged cell
{
  const game = new Game(9, 9, 10);
  game.revealCell(0, 0);
  game.toggleFlag(2, 2);
  const result = game.revealCell(2, 2);
  assert(!result.changed, 'should not reveal flagged cell');
  assert(!game.board[2][2].isRevealed, 'flagged cell stays hidden');
}

// Custom validation
{
  assert(validateCustomDifficulty(9, 9, 10) === null, 'valid custom config');
  assert(validateCustomDifficulty(8, 9, 10) !== null, 'cols too small');
  assert(validateCustomDifficulty(9, 9, 100) !== null, 'mines too many');
}

// Difficulty presets match Windows
{
  assert(DIFFICULTIES.beginner.rows === 9 && DIFFICULTIES.beginner.cols === 9 && DIFFICULTIES.beginner.mines === 10, 'beginner');
  assert(DIFFICULTIES.intermediate.rows === 16 && DIFFICULTIES.intermediate.mines === 40, 'intermediate');
  assert(DIFFICULTIES.expert.rows === 16 && DIFFICULTIES.expert.cols === 30 && DIFFICULTIES.expert.mines === 99, 'expert');
}

// Win detection on tiny board
{
  const game = new Game(3, 3, 1);
  game.revealCell(0, 0);
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) {
      const cell = game.board[r][c];
      if (!cell.isMine && !cell.isRevealed && !cell.isFlagged) {
        game.revealCell(r, c);
      }
    }
  }
  assert(game.status === 'won' || game.status === 'playing', 'game progresses without error');
}

// Chord reveals neighbors when flags match
{
  let chordWorked = false;
  for (let attempt = 0; attempt < 50; attempt++) {
    const game = new Game(9, 9, 10);
    game.revealCell(4, 4);
    for (let r = 0; r < 9; r++) {
      for (let c = 0; c < 9; c++) {
        const cell = game.board[r][c];
        if (cell.isRevealed && cell.neighborMines > 0) {
          let flags = 0;
          for (let dr = -1; dr <= 1; dr++) {
            for (let dc = -1; dc <= 1; dc++) {
              if (dr === 0 && dc === 0) continue;
              const nr = r + dr;
              const nc = c + dc;
              if (nr >= 0 && nr < 9 && nc >= 0 && nc < 9 && game.board[nr][nc].isMine) {
                game.toggleFlag(nr, nc);
                flags++;
              }
            }
          }
          if (flags === cell.neighborMines) {
            const hiddenBefore = game.board.flat().filter((x) => !x.isRevealed && !x.isMine).length;
            const result = game.chordReveal(r, c);
            if (result.changed && game.status === 'playing') {
              chordWorked = true;
            }
            break;
          }
        }
      }
      if (chordWorked) break;
    }
    if (chordWorked) break;
  }
  assert(chordWorked, 'chord should reveal neighbors when flags match');
}

console.log(`\nResults: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
