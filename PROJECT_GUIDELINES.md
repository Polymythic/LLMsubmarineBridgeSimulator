# Project Guidelines

1. **Testing:**  
   - Every new feature must have unit tests in `/tests` folder.
   - Run tests before every commit.

2. **Documentation:**  
   - Update `README.md` with new commands or dependencies.
   - Maintain `CHANGELOG.md` for user-facing updates.

3. **Code Style:**  
   - Follow PEP8 for Python.
   - Prefer functional composition over large classes unless justified.

4. **Architecture Notes:**  
   - Keep the game loop pure; side effects only in `io_handlers.py`.
   - Sprite assets go in `/assets/sprites`.

5. **Conventions:**
   - Bearings are compass bearings: 0°=North, 90°=East, 180°=South, 270°=West. Use `atan2(dx, dy)` when converting (note the argument order).
   - UI: Keep station telemetry compact; surface only what the station needs.
   - Debug actions like `debug.restart` must reinitialize to the same default world as startup.
