from datetime import datetime


def timestamp_to_window(timestamp, start_time, end_time, window_size):
    return timestamp - timestamp % window_size
    # if timestamp > end_time:
    #     timestamp = end_time
    # window_start_time = ((timestamp - start_time) // window_size) * window_size + start_time
    # return window_start_time


def convert_timestamp(timestamp):
    if timestamp > 1e12:
        timestamp = timestamp / 1000.0
    dt_object = datetime.fromtimestamp(timestamp)
    formatted_time = dt_object.strftime("%Y-%m-%d %H:%M:%S")
    return formatted_time