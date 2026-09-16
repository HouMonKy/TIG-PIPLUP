#!/usr/bin/python3.7
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2012-2020. All rights reserved.
import threading


class ReadWriteLock:
    def __init__(self):
        self.read_lock = threading.Lock()  # 用于保护读计数器的锁
        self.write_lock = threading.Lock()  # 写锁，确保同一时刻只有一个线程可以写
        self.condition = threading.Condition(self.read_lock)  # 读写锁条件变量
        self.readers = 0  # 当前读锁持有者的数量
        self.write_waiting = 0  # 等待获取写锁的线程数量
        self.write_active = False  # 当前是否有写线程正在持有写锁

    # 获取读锁
    def acquire_read_lock(self):
        with self.condition:
            # 如果有写线程正在排队或者活跃，等待读锁
            while self.write_waiting > 0 or self.write_active:
                self.condition.wait()  # 线程休眠，等待条件变量

            self.readers += 1
            if self.readers == 1:
                # 第一个读者获取锁时，阻塞写操作
                self.write_lock.acquire()

    # 释放读锁
    def release_read_lock(self):
        with self.condition:
            self.readers -= 1
            if self.readers == 0:
                # 最后一个读者释放锁时，允许写操作
                self.write_lock.release()

            # 通知可能等待的写线程
            self.condition.notify_all()

    # 获取写锁
    def acquire_write_lock(self):
        with self.condition:
            self.write_waiting += 1  # 等待写锁的线程增加
            while self.readers > 0 or self.write_active:
                # 如果有读线程或者写线程活跃，等待写锁
                self.condition.wait()

        self.write_lock.acquire()  # 获取写锁

        with self.condition:
            self.write_waiting -= 1  # 写锁获取后，减少排队线程

        # 标记当前有写线程
        self.write_active = True

    # 释放写锁
    def release_write_lock(self):
        with self.condition:
            self.write_active = False  # 标记写线程已释放锁
            self.write_lock.release()  # 释放写锁

            # 通知等待的读线程和写线程
            self.condition.notify_all()


class FairLock:
    def __init__(self):
        self._lock = threading.Lock()  # 保护临界区的原子锁
        self._condition = threading.Condition(self._lock)  # 用于通知
        self._waiters = []  # 存储线程的ID，模拟等待队列

    def acquire(self):
        with self._condition:
            my_id = threading.get_ident()  # 获取当前线程的ID
            self._waiters.append(my_id)  # 当前线程加入等待队列
            while self._waiters[0] != my_id:  # 如果当前线程不是第一个
                self._condition.wait()  # 等待直到它是第一个线程

    def release(self):
        with self._condition:
            my_id = threading.get_ident()  # 获取当前线程的ID
            if self._waiters and self._waiters[0] == my_id:  # 确保是排在最前的线程
                self._waiters.pop(0)  # 移除已经获得锁的线程ID
                self._condition.notify()  # 唤醒下一个线程

    # 使得 FairLock 可以作为上下文管理器（支持 with 语句）
    def __enter__(self):
        self.acquire()  # 在进入时调用 acquire 获取锁
        return self  # 可以返回锁对象本身，也可以返回其他对象，通常返回锁对象

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()  # 在退出时调用 release 释放锁
