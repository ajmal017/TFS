import configparser
import pdb
import sys
import logging
import decimal
from optparse import OptionParser
import pandas as pd
# from datetime import datetime, time
import datetime
import time
from utils import futures

from utils.strategies import TFS, Unit
from utils.driver import Driver
from utils.sod import SOD
from db.database import Database

from ib import ib
from ibapi.contract import Contract

from pytz import timezone

from config import tfslog

EOD_NY_TIME = datetime.time(16, 0)
SOD_NY_TIME = datetime.time(9, 30)

if __name__ == '__main__':
    # Check that the port is the same as on the Gateway
    # ipaddress is 127.0.0.1 if one same machine, clientid is arbitrary

    # setup timezones
    utc_tz = timezone('UTC')
    eastern_tz = timezone('US/Eastern')
    amsterdam_tz = timezone('Europe/Amsterdam')
    fmt = '%Y-%m-%d %H:%M:%S %Z%z'

    # set up logging
    # setup log
    tfslog.setup_logging()
    logger = logging.getLogger()
    logger.info("create logger object")

    # read config file
    config = configparser.ConfigParser()
    config.read('config/settings.cfg')

    max_units = int(config['tfs']['max_units'])

    futures_list = {}
    for f in config.items('futures_list'):
        futures_list[f[0].upper()] = f[1]

    futures_utils = futures.FuturesUtils()
    expiration_dates = futures_utils.resolve_expiration_month_codes()
    expiration_month_codes = expiration_dates[0]
    expiration_months = expiration_dates[1]

    atr_horizon = int(config['tfs']['atr_horizon'])
    entry_breakout_periods = int(config['tfs']['entry_breakout_periods'])
    exit_breakout_periods = int(config['tfs']['exit_breakout_periods'])
    account_risk = decimal.Decimal(config['tfs']['account_risk'])
    unit_stop = int(config['tfs']['unit_stop'])
    first_unit_stop = int(config['tfs']['first_unit_stop'])
    nr_equities = int(config['tfs']['nr_equities'])
    nr_units = int(config['tfs']['nr_units'])

    # parse arguments
    parser = OptionParser()
    parser.add_option("-e", "--eod", action="store_true", default=False,
                      dest="eod", help="Perform end of day actions.")
    parser.add_option("-t", "--test", action="store_true", default=False,
                      dest="test", help="Run program in test mode.")
    (options, args) = parser.parse_args()
    eod = options.eod
    test_mode = options.test

    db = Database()
    tfs_strat = TFS()
    driver = Driver()

    settings_from_db = db.get_settings_from_db(
        ('masterid',))

    try:
        app = ib.IB("127.0.0.1", 4011, int(settings_from_db['masterid']))
    except AttributeError as exp:
        print("Could not connect to the TWS API application.")
        sys.exit()

    current_time = app.get_time()

    minute_interval = 5
    second_interval = 5
    eod_job_started = False
    sod_job_started = False

    # retrieve account data
    account_numbers = db.get_settings_from_db(
        ('live_account', 'simu_account'))
    live_acct_nbr = account_numbers['live_account']

    account_info = driver.get_account_data(app)
    if account_info is not None:
        buying_power = account_info[0]
        account_size = account_info[1]
        connected_account_number = account_info[2]

    while(True):
        time.sleep(0.2)  # prevent the CPU from going wild
        curr_time = datetime.datetime.now().time()
        curr_utc_time = datetime.datetime.now(utc_tz)
        curr_ams_time = curr_utc_time.astimezone(amsterdam_tz).time()
        curr_ny_time = curr_utc_time.astimezone(eastern_tz).time()

        if (SOD_NY_TIME < curr_ny_time < EOD_NY_TIME and sod_job_started is False):
            logger.info("It's %s. Markets have opened in New York." %
                        (curr_ams_time))

            app.init_error()
            sod_job_started = True

            try:
                sod = SOD(app)
                sod.start(live_acct_nbr, connected_account_number)
            except Exception:
                logger.error(Exception, exc_info=True)

        elif (curr_ny_time > EOD_NY_TIME and eod_job_started is False) \
                or test_mode:
            logger.info("Starting EOD process at %s." %
                        curr_ams_time)

            app.init_error()
            eod_job_started = True

            # retrieve current exchange rate data
            hist_data = []
            for instr in config.items('forex'):
                forex_data = driver.get_historical_data(
                    app,
                    instr,
                    "1 D")
                hist_data.append(forex_data)

            eod_data = tfs_strat.eod_data(
                ib=app,
                portfolio_list=config.items('portfolio'),
                tfs_settings=config['tfs'],
                account_size=account_size)

            # add stop orders to eod data
            new_dataset = driver.add_stop_orders(eod_data, app)
            eod_data = new_dataset[0]
            eod_data = driver.add_columns(eod_data, ['next_price_target'])
            driver.update_stop_orders(new_dataset)

            """
            prepared_orders = driver.prepare_orders(
                eod_data,
                config.items('portfolio'))
            """

            for index, row in eod_data.iterrows():
                # https://stackoverflow.com/questions/25478528/updating-value-in-iterrow-for-pandas
                recommendations, updated_row = \
                    driver.spot_trading_opportunities(
                        row,
                        config['tfs'],
                        account_size,
                        config.items('portfolio'))
                eod_data.loc[index, 'stop_price'] = updated_row['stop_price']
                eod_data.loc[index, 'next_price_target'] = \
                    updated_row['next_price_target']

            try:
                chart = driver.draw_bulletgraph(eod_data)
            except Exception as e:
                logging.error("error generating bullet graph: ", e)

            # store account numbers in database
            date = eod_data.iloc[0, eod_data.columns.get_loc('date')]
            db.insert_account_numbers(date, account_size, buying_power)

            # create report
            # TODO make code to create reports

            print("=============================")
            print("Account size: ", account_size)
            print("=============================")
            print("Forex market data:\n")
            print(forex_data)
            print("=============================")
            print("Daily recap:\n", eod_data)
            shorts = eod_data.loc[eod_data['close'] < eod_data['55DayLow']]
            if shorts.shape[0] > 0:
                print("=============================")
                print("Potential short candidates:\n\n",
                      shorts[['date', 'close', 'atr',
                              'pos_size (1st)']])
            longs = eod_data.loc[eod_data['close'] > eod_data['55DayHigh']]
            if longs.shape[0] > 0:
                print("=============================")
                print("Potential long candidates:\n\n",
                      longs[['date', 'close', 'atr',
                             'pos_size (1st)']])

            print("\n=============================\n")

            # n_pos_instrument = db.get_position_size(ticker)
        elif (curr_ny_time < EOD_NY_TIME and curr_ny_time < SOD_NY_TIME):
            eod_job_started = False
            sod_job_started = False

        # store stuff in Database
        # blabla

    if 1 == 2:
        for future in futures_list:
            future_meta_data = futures_list[future].split(',')
            description_future = future_meta_data[0]
            exchange_future = future_meta_data[1].lstrip()
            security_type = future_meta_data[2].lstrip()
            for month in expiration_months:
                ibcontract = Contract()
                # ibcontract.secType = security_type
                # ibcontract.symbol = future.upper()
                # ibcontract.exchange = exchange_future
                # ibcontract.lastTradeDateOrContractMonth = month

                ibcontract.secType = "FUT"
                ibcontract.symbol = "GE"
                ibcontract.exchange = "GLOBEX"
                ibcontract.lastTradeDateOrContractMonth = "201812"

                resolved_ibcontract = app.resolve_ib_contract(ibcontract)
                if resolved_ibcontract is not None:
                    print(resolved_ibcontract)
                    tickerid = app.start_getting_IB_market_data(resolved_ibcontract)
                    time.sleep(30)

                    # What have we got so far?
                    market_data1 = app.get_IB_market_data(tickerid)

                    try:
                        print(market_data1[0])
                        market_data1_as_df = market_data1.as_pdDataFrame()
                        print(market_data1_as_df)

                        time.sleep(30)

                        # stops the stream and returns all the data we've got so far
                        market_data2 = app.stop_getting_IB_market_data(tickerid)

                        # glue the data together
                        market_data2_as_df = market_data2.as_pdDataFrame()
                        all_market_data_as_df = pd.concat([market_data1_as_df, market_data2_as_df])

                        # show some quotes
                        some_quotes = all_market_data_as_df.resample("1S").last(
                        )[["bid_size", "bid_price", "ask_price", "ask_size"]]
                        print(some_quotes.head(10))

                        # show some trades
                        some_trades = all_market_data_as_df.resample(
                            "10L").last()[["last_trade_price", "last_trade_size"]]
                        print(some_trades.head(10))
                    except:
                        print('iets niet lekker gegaan')

                    input('stop even')

                    # historic_data = app.get_IB_historical_data(resolved_ibcontract)
                    # if historic_data is not None:
                    # df = pd.DataFrame(historic_data)
                    # print(df) # voor later

    print('\n\nFinished.')
