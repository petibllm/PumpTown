# Simulator rules

The defaults live at the top of `village/executor.py`. Change them there to test your own ideas.

Every account starts with 1 SOL. Trades are simulated on live prices, and fills include fees, price impact and a priority fee.

## Entry

| Rule | Default |
|---|---|
| Grade | D or better, graded in the last 20 minutes |
| Market | pump.fun curve or PumpSwap only |
| Coin age when graded | at least 30 minutes |
| Market cap | at least $7,000 |
| Volume in the last 5 minutes | at least 5 SOL |
| Developer holding | at most 5% of supply (exact with a Solana RPC key, otherwise estimated from recorded trades) |
| Buy pressure | buys at least 80% of sells, 5 or more trades |
| Rise in the last 5 minutes | at most 40% |
| Open positions | at most 3 |
| Position size | 5% of balance x grade (D 0.4, C 1, B and up 1.5), at most 0.25 SOL |

## Exit

| Rule | Default |
|---|---|
| Stop loss | -20%, before the stake is back |
| Stake back | at +50%, sell enough to recover the stake |
| Take profit | at +150%, sell half of the rest |
| Trailing exit | 30% below the peak, after the stake is back |
| Time limit | 1 hour, or 3 hours after the stake is back |

Stops and trailing exits are checked every 3 seconds. Everything else is checked every 30 seconds.

## Risk limits

| Rule | Default |
|---|---|
| Daily stop | no new trades after -0.3 SOL in a day |
| Losing streak | 4 losses in a row pauses buying for 2 hours |

## Grades

| Grade | Score |
|---|---|
| F | 0-34 |
| E | 35-49 |
| D | 50-61 |
| C | 62-71 |
| B | 72-81 |
| A | 82-91 |
| S | 92+ |

A grade rates the evidence on a coin. It does not predict the price.
