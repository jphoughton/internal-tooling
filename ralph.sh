#!/bin/bash

# Ralph Loop — headless Claude Code for cash flow model fixes
# Each iteration: read PRD, find next unchecked task, implement it, test, commit, update progress

MAX_ITERATIONS=30
CURRENT=1

while [ $CURRENT -le $MAX_ITERATIONS ]; do
    echo ""
    echo "========================================="
    echo "  Ralph Loop — Iteration $CURRENT of $MAX_ITERATIONS"
    echo "========================================="
    echo ""

    # Run Claude Code in headless mode with a strict prompt
    claude -p "Read PRD.md and progress.txt. Find the next unchecked task in PRD.md. Implement it. Run tests if applicable (pytest tests/ -x). If tests pass (or no tests are broken), commit the code to git with a conventional commit message. Update progress.txt with what you did and any issues encountered. Mark the task as checked in PRD.md by changing '- [ ]' to '- [x]'. ONLY DO ONE TASK, THEN STOP."

    # Check if all tasks are done
    REMAINING=$(grep -c '^\- \[ \]' PRD.md 2>/dev/null || echo "0")
    if [ "$REMAINING" -eq "0" ]; then
        echo ""
        echo "All tasks complete! Ralph loop finished."
        exit 0
    fi

    CURRENT=$((CURRENT+1))
done

echo ""
echo "Ralph loop hit max iterations ($MAX_ITERATIONS). $REMAINING tasks remaining."
