#!/usr/bin/python3

import sys
import pandas as pd
import numpy as np
import os
import concurrent.futures
import functools, itertools
import sofa_time
import statistics
import multiprocessing as mp
import socket
import ipaddress

sys.path.insert(0, '/home/st9540808/Desktop/VS_Code/sofa/bin')
import sofa_models, sofa_preprocess

colors = ['DeepPink', 'RebeccaPurple', 'RoyalBlue', 'MediumSlateBlue']
color = itertools.cycle(colors)

def extract_individual_rosmsg(df_send, df_recv, *df_others):
    """ Return a dictionary with topic name as key and
        a list of ros message as value.
        Structure of return value: {topic_name: {(guid, seqnum): log}}
        where (guid, seqnum) is a msg_id
    """
    # Convert timestamp to unix time
    unix_time_off = statistics.median(sofa_time.get_unix_mono_diff() for i in range(100))
    for df in (df_send, df_recv, *df_others):
        df['ts'] = df['ts'] + unix_time_off

    # sort by timestamp
    df_send.sort_values(by=['ts'], ignore_index=True)
    df_recv.sort_values(by=['ts'], ignore_index=True)

    # publish side
    gb_send = df_send.groupby('guid')
    all_publishers_log = {guid:log for guid, log in gb_send}

    # subscription side
    gb_recv = df_recv.groupby('guid')
    all_subscriptions_log = {guid:log for guid, log in gb_recv}

    # other logs (assume there's no happen-before relations that needed to be resolved)
    # every dataframe is a dictionary in `other_log_list`
    gb_others = [df_other.groupby('guid') for df_other in df_others]
    other_log_list = [{guid:log for guid, log in gb_other} for gb_other in gb_others]

    # find guids that are in both subsciption and publisher log
    interested_guids = all_subscriptions_log.keys() \
                     & all_publishers_log.keys()

    res = {}
    for guid in interested_guids:
        # get a publisher from log
        df = all_publishers_log[guid]
        df_send_partial = all_publishers_log[guid].copy()
        add_data_calls = df[~pd.isna(df['seqnum'])] # get all non-NaN seqnums in log
        try:
            pubaddr, = pd.unique(df['publisher']).dropna()
        except ValueError as e:
            print('Find a guid that is not associated with a publisher memory address. Error: ' + str(e))
            continue
        # print(add_data_calls)

        all_RTPSMsg_idx = ((df_send['func'] == '~RTPSMessageGroup') & (df_send['publisher'] == pubaddr))
        all_RTPSMsgret_idx = ((df_send['func'] == '~RTPSMessageGroup exit') & (df_send['publisher'] == pubaddr))
        all_sendSync_idx = ((df_send['func'] == 'sendSync') & (df_send['publisher'] == pubaddr))
        modified_rows = []
        for idx, add_data_call in add_data_calls.iterrows():
            ts = add_data_call['ts']

            rcl_idx = df.loc[(df['ts'] < ts) & (df['layer'] == 'rcl')]['ts'].idxmax()
            df_send_partial.loc[rcl_idx, 'seqnum'] = add_data_call.loc['seqnum']

            # For grouping RTPSMessageGroup function
            try:
                ts_gt = (df_send['ts'] > ts) # ts greater than that of add_data_call

                RTPSMsg_idx = df_send.loc[ts_gt & all_RTPSMsg_idx]['ts'].idxmin()
                modified_row = df_send.loc[RTPSMsg_idx]
                modified_row.at['seqnum'] = add_data_call.loc['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)

                RTPSMsgret_idx = df_send.loc[ts_gt & all_RTPSMsgret_idx]['ts'].idxmin()
                modified_row = df_send.loc[RTPSMsgret_idx]
                modified_row.at['seqnum'] = add_data_call.loc['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)

                sendSync_idx = df_send.loc[ts_gt & (df_send['ts'] < df_send.loc[RTPSMsgret_idx, 'ts']) & all_sendSync_idx]
                sendSync = sendSync_idx.copy()
                sendSync['seqnum'] = add_data_call.loc['seqnum']
                modified_rows.extend(row for _, row in sendSync.iterrows())
            except ValueError as e:
                pass
        df_send_partial = pd.concat([df_send_partial, pd.DataFrame(modified_rows)])

        # get a subscrption from log
        df = all_subscriptions_log[guid]
        df_recv_partial = all_subscriptions_log[guid].copy()
        add_recvchange_calls = df[~pd.isna(df['seqnum'])] # get all not nan seqnums in log

        all_sub = pd.unique(df['subscriber']) # How many subscribers subscribe to this topic?
        subs_map = {sub: (df['subscriber'] == sub) &
                         (df['func'] == "rmw_take_with_info exit") for sub in all_sub}
        all_pid = pd.unique(df_recv['pid'])
        pid_maps = {pid: (df_recv['pid'] == pid) &
                         (df_recv['func'] == "rmw_wait exit") for pid in all_pid}
        modified_rows = []
        for idx, add_recvchange_call in add_recvchange_calls.iterrows():
            ts = add_recvchange_call['ts']
            subaddr = add_recvchange_call.at['subscriber']

            # Consider missing `rmw_take_with_info exit` here
            try:
                rmw_take_idx = df.loc[(df['ts'] > ts) & subs_map[subaddr]]['ts'].idxmin()
                df_recv_partial.at[rmw_take_idx, 'seqnum'] = add_recvchange_call.loc['seqnum']

                # Group rmw_wait
                pid = df_recv_partial.at[rmw_take_idx, 'pid']
                rmw_wait_idx = df_recv.loc[(df_recv['ts'] > ts) & pid_maps[pid]]['ts'].idxmin()
                modified_row = df_recv.loc[rmw_wait_idx]
                modified_row.at['seqnum'] = add_recvchange_call.at['seqnum']
                modified_row.at['guid'] = guid
                modified_rows.append(modified_row)
            except ValueError as e:
                pass
        df_recv_partial = pd.concat([df_recv_partial, pd.DataFrame(modified_rows)])

        # Merge all modified dataframes
        df_merged = df_send_partial.append(df_recv_partial, ignore_index=True, sort=False)

        # handle other log files
        for other_log in other_log_list:
            df_other = other_log[guid]
            df_merged = df_merged.append(df_other, ignore_index=True, sort=False)

        # Avoid `TypeError: boolean value of NA is ambiguous` when calling groupby()
        df_merged['subscriber'] = df_merged['subscriber'].fillna(np.nan)
        df_merged['guid'] = df_merged['guid'].fillna(np.nan)
        df_merged['seqnum'] = df_merged['seqnum'].fillna(np.nan)
        df_merged.sort_values(by=['ts'], inplace=True)
        gb_merged = df_merged.groupby(['guid', 'seqnum'])

        ros_msgs = {msg_id:log for msg_id, log in gb_merged} # msg_id: (guid, seqnum)
        # get topic name from log
        topic_name = df_merged['topic_name'].dropna().unique()
        if len(topic_name) > 1:
            raise Exception("More than one topic in a log file")
        topic_name = topic_name[0]
        res[topic_name] = ros_msgs
        print('finished parsing ' + topic_name)
    return res

def print_all_msgs(res):
    for topic_name, all_msgs_log in res.items():
        print('topic: ' + topic_name)
        for (guid, seqnum), msg_log in all_msgs_log.items():
            print('msg_id: ', (guid, seqnum))
            print(msg_log)
            print('')

def ros_msgs_trace_read(items, cfg):
    sofa_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        # msg_log['subscriber'] = msg_log['subscriber'].apply(lambda x: np.nan if x is pd.NA else x)
        gb_sub = msg_log.groupby('subscriber') # How many subscribers receviced this ros message?
        start = msg_log.iloc[0]
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        for sub_addr, sub_log in gb_sub:
            trace = dict(zip(sofa_fieldnames, itertools.repeat(-1)))
            end = sub_log.iloc[-1]
            if end.at['layer'] != 'rmw': # skip when the last function call is not from rmw (eg. rosbag2)
                continue

            time = start['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = start['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (end['ts'] - start['ts']) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Transmission: %s -> %s" % \
                            (start['layer'], start['func'], end['layer'], end['func'],
                             start['topic_name'],
                             start['comm'], end['comm'])
            trace['unit'] = 'ms'
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_os_lat(items, cfg):
    sofa_fieldnames = [
        "timestamp",  # 0
        "event",      # 1
        "duration",   # 2
        "deviceId",   # 3
        "copyKind",   # 4
        "payload",    # 5
        "bandwidth",  # 6
        "pkt_src",    # 7
        "pkt_dst",    # 8
        "pid",        # 9
        "tid",        # 10
        "name",       # 11
        "category",   # 12
        "unit"]

    traces = []
    topic_name, all_msgs_log = items
    for msg_id, msg_log in all_msgs_log.items():
        start = msg_log.iloc[0]
        if start.at['layer'] != 'rcl': # skip when the first function call is not from rcl
            continue

        all_sendSync = msg_log.loc[msg_log['func'] == 'sendSync'].copy()
        all_egress = msg_log.loc[msg_log['layer'] == 'cls_egress']

        for _, sendSync in all_sendSync.iterrows():
            trace = dict(zip(sofa_fieldnames, itertools.repeat(-1)))
            addr = sendSync['addr']
            port = sendSync['port']
            egress = all_egress.loc[(all_egress['addr'] == addr) & (all_egress['port'] == port)].iloc[0]


            time = sendSync['ts']
            if cfg is not None and not cfg.absolute_timestamp:
                time = sendSync['ts'] - cfg.time_base
            trace['timestamp'] = time
            trace['duration'] = (egress['ts'] - sendSync['ts']) * 1e3 # ms
            trace['name'] = "[%s] %s -> [%s] %s <br>Topic Name: %s<br>Destination address: %s:%d" % \
                            (sendSync['layer'], sendSync['func'], egress['layer'], '',
                             start['topic_name'],
                             str(ipaddress.IPv4Address(socket.ntohl(int(addr)))),
                             socket.ntohs(int(port)))
            trace['unit'] = 'ms'
            traces.append(trace)
    traces = pd.DataFrame(traces)
    return traces

def ros_msgs_trace_read_dds_lat(items, cfg):
    pass

def run(cfg):
    """ Start preprocessing. """
    # Read all log files generated by ebpf_ros2_*
    # TODO: convert addr and port to uint32, uint16
    read_csv = functools.partial(pd.read_csv, dtype={
        'pid':'Int32', 'seqnum':'Int64', 'subscriber':'Int64', 'publisher':'Int64'})
    cvs_files_others = ['cls_bpf_log.csv']
    df_send = read_csv('send_log.csv')
    df_recv = read_csv('recv_log.csv')
    df_others = []
    for csv_file in cvs_files_others:
        try:
            df_others.append(read_csv(csv_file))
        except pd.errors.EmptyDataError as e:
            print(csv_file + ' is empty')

    all_msgs = extract_individual_rosmsg(df_send, df_recv, *df_others)
    # print(all_msgs)

    # TODO: Filiter topics

    # Calculate ros latency for all topics
    res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            res.append(future.result())
    print(res)
    # res = ros_msgs_trace_read(next(iter(all_msgs.items())), cfg=cfg)

    # Calculate time spent in OS for all topics
    os_lat = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        future_res = {executor.submit(ros_msgs_trace_read_os_lat, item, cfg=cfg): item for item in all_msgs.items()}
        for future in concurrent.futures.as_completed(future_res):
            item = future_res[future]
            topic = item[0]
            os_lat.append(future.result())
    print(os_lat)

    # dds_lat = []
    # with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
    #     future_res = {executor.submit(ros_msgs_trace_read_dds_lat, item, cfg=cfg): item for item in all_msgs.items()}
    #     for future in concurrent.futures.as_completed(future_res):
    #         item = future_res[future]
    #         topic = item[0]
    #         dds_lat.append(future.result())
    # print(dds_lat)

    sofatrace = sofa_models.SOFATrace()
    sofatrace.name = 'ros2_latency'
    sofatrace.title = 'ros2_latency'
    sofatrace.color = next(color)
    sofatrace.x_field = 'timestamp'
    sofatrace.y_field = 'duration'
    sofatrace.data = pd.concat(res) # TODO:

    sofatrace_os_lat = sofa_models.SOFATrace()
    sofatrace_os_lat.name = 'os_send_latency'
    sofatrace_os_lat.title = 'os_send_latency'
    sofatrace_os_lat.color = next(color)
    sofatrace_os_lat.x_field = 'timestamp'
    sofatrace_os_lat.y_field = 'duration'
    sofatrace_os_lat.data = pd.concat(os_lat)

    # cmd_vel = all_msgs['/cmd_vel']
    # cmd_vel_msgids = [('1.f.c5.ba.f4.30.0.0.1.0.0.0|0.0.10.3', num) for num in [46, 125, 170, 208, 269, 329, 545, 827, 918, 1064, 1193, 1228, 1282]]
    # print(cmd_vel[('1.f.c5.ba.f4.30.0.0.1.0.0.0|0.0.10.3', 45)])
    # res2 = ros_msgs_trace_read(('/cmd_vel', {msgid:cmd_vel[msgid] for msgid in cmd_vel_msgids}), cfg=cfg)
    # highlight = sofa_models.SOFATrace()
    # highlight.name = 'update_cmd_vel'
    # highlight.title = 'Change velocity event'
    # highlight.color = next(color)
    # highlight.x_field = 'timestamp'
    # highlight.y_field = 'duration'
    # highlight.data = pd.concat([res2])

    return [sofatrace, sofatrace_os_lat]

if __name__ == "__main__":
    # read_csv = functools.partial(pd.read_csv, dtype={'pid':'Int32', 'seqnum':'Int64'})
    # df_send = read_csv('send_log.csv')
    # df_cls = read_csv('cls_bpf_log.csv')
    # df_recv = read_csv('recv_log.csv')
    # res = extract_individual_rosmsg(df_send, df_recv, df_cls)
    # print_all_msgs(res)

    res = run(None)
    for r in res:
        print(r.data)