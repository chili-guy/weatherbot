"""Training loop — resolve → calibrate → backtest.

Closes the forecast-to-truth loop by reading ERA5 observations for markets we
already analyzed, aggregating (realized − forecast_mean) into a bias table,
and scoring past recommendations against realized outcomes.
"""
