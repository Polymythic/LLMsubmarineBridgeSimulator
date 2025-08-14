# Overview
- Enemy ship strategy is driven by LLM calls that conform to the OpenAI API spec
- The LLM to use should be able to be specified in the debug tab
- The LLMs should be able to invoke LLAMA (local LLM) or OpenAI (remote LLM)
- There is an independent LLM session per ship
- The LLM AI invocation should execute every 20 seconds if the sub is not detected, but should execute every 10 seconds if the sub is detected.  This is to allow the LLM to plan ahead and make decisions based on the current state of the game.
- The LLM should be able to make calls to the game state to determine the current state of the game.
- The LLM should be able to make calls to the enemy fleet state to determine the current state of the enemy fleet.
- The LLM should be able to make calls to the enemy ship state to determine the current state of the enemy ship.


# Enenmy AI Strategy
- One LLM is able to acts as the fleet commander.  The model should be able to make calls to the best reasoning and slowest response model to plan at the high level.
- The fleet commander should report the fleet strategy to the game state, so all ships can see the fleet strategy.
- Each ship should be able to have specified to it in the debug tab a prompt that will be used to drive the ship's AI.
- Each ship should have an LLM session that can call into the game state to determine its position, speed, detectability, and so on.  It should also be able to ask the game state on what that ship would know about the enemy fleet.
- The fleet commander should be able to make calls to the ships to determine their actions.