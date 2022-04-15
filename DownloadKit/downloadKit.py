# -*- coding:utf-8 -*-
"""
@Author  :   g1879
@Contact :   g1879@qq.com
@File    :   downloadKit.py
"""
from pathlib import Path
from queue import Queue
from re import sub
from threading import Thread, Lock
from time import sleep, perf_counter
from typing import Union
from urllib.parse import quote, urlparse

from DataRecorder import Recorder
from requests import Session, Response
from requests.structures import CaseInsensitiveDict

from ._funcs import FileExistsSetter, PathSetter, BlockSizeSetter, copy_session, \
    _set_charset, _get_file_info
from .mission import Task, Mission


class DownloadKit(object):
    file_exists = FileExistsSetter()
    goal_path = PathSetter()
    block_size = BlockSizeSetter()

    def __init__(self,
                 goal_path: Union[str, Path] = None,
                 roads: int = 10,
                 session=None,
                 timeout: float = None,
                 file_exists: str = 'rename'):
        """初始化                                                                         \n
        :param goal_path: 文件保存路径
        :param roads: 可同时运行的线程数
        :param session: 使用的Session对象，或配置对象、页面对象等
        :param timeout: 连接超时时间
        :param file_exists: 有同名文件名时的处理方式，可选 'skip', 'overwrite', 'rename'
        """
        self._roads = roads
        self._missions = {}
        self._threads = {i: None for i in range(self._roads)}
        self._waiting_list: Queue = Queue()
        self._missions_num = 0
        self._stop_printing = False  # 用于控制显示线程停止
        self._lock = Lock()
        self._page = None  # 如果接收MixPage对象则存放于此

        self.goal_path: str = goal_path or '.'
        self.retry: int = 3
        self.interval: float = 5
        self.timeout: float = timeout if timeout is not None else 20
        self.file_exists: str = file_exists
        self.show_errmsg: bool = False
        self.block_size: Union[str, int] = '20M'  # 分块大小
        self.session = session

    def __call__(self,
                 file_url: str,
                 goal_path: Union[str, Path] = None,
                 rename: str = None,
                 file_exists: str = None,
                 post_data: Union[str, dict] = None,
                 show_msg: bool = True,
                 **kwargs) -> tuple:
        """以阻塞的方式下载一个文件并返回结果，主要用于兼容旧版DrissionPage                                     \n
        :param file_url: 文件网址
        :param goal_path: 保存路径
        :param session: 用于下载的Session对象，默认使用实例属性的
        :param rename: 重命名的文件名
        :param file_exists: 遇到同名文件时的处理方式，可选 'skip', 'overwrite', 'rename'，默认跟随实例属性
        :param post_data: post方式使用的数据
        :param show_msg: 是否打印进度
        :param kwargs: 连接参数
        :return: 任务结果和信息组成的tuple
        """
        return self.add(file_url=file_url,
                        goal_path=goal_path,
                        rename=rename,
                        file_exists=file_exists,
                        post_data=post_data,
                        split=False,
                        **kwargs).wait(show=show_msg)

    @property
    def roads(self) -> int:
        """可同时运行的线程数"""
        return self._roads

    @roads.setter
    def roads(self, val: int) -> None:
        """设置roads值"""
        if self.is_running():
            print('有任务未完成时不能改变roads。')
            return
        if val != self._roads:
            self._roads = val
            self._threads = {i: None for i in range(self._roads)}

    @property
    def waiting_list(self) -> Queue:
        """返回等待队列"""
        return self._waiting_list

    @property
    def session(self) -> Session:
        return self._session

    @session.setter
    def session(self, session) -> None:
        try:
            from DrissionPage import Drission, MixPage
            from DrissionPage.session_page import SessionPage
            from DrissionPage.config import SessionOptions

            if isinstance(session, SessionOptions):
                self._session = Drission(driver_or_options=False, session_or_options=session).session
            elif isinstance(session, Drission):
                self._session = session.session
            elif isinstance(session, (MixPage, SessionPage)):
                self._session = session.session
                self._page = session
                self.retry = session.retry_times
                self.interval = session.retry_interval
            else:
                self._session = Drission(driver_or_options=False).session

        except ImportError:
            self._session = Session()

    def is_running(self) -> bool:
        """检查是否有线程还在运行中"""
        return any(self._threads.values()) or not self.waiting_list.empty()

    def add(self,
            file_url: str,
            goal_path: Union[str, Path] = None,
            session: Session = None,
            rename: str = None,
            file_exists: str = None,
            post_data: Union[str, dict] = None,
            split: bool = True,
            **kwargs) -> Mission:
        """添加一个下载任务并将其返回                                                                    \n
        :param file_url: 文件网址
        :param goal_path: 保存路径
        :param session: 用于下载的Session对象，默认使用实例属性的
        :param rename: 重命名的文件名
        :param file_exists: 遇到同名文件时的处理方式，可选 'skip', 'overwrite', 'rename'，默认跟随实例属性
        :param post_data: post方式使用的数据
        :param split: 是否允许多线程分块下载
        :param kwargs: 连接参数
        :return: 任务对象
        """
        session = session or self.session
        session.stream = True
        data = {'file_url': file_url,
                'goal_path': str(goal_path or self.goal_path),
                'session': session,
                'rename': rename,
                'file_exists': file_exists or self.file_exists,
                'post_data': post_data,
                'split': split,
                'kwargs': kwargs}
        self._missions_num += 1
        mission = Mission(self._missions_num, data)
        self._missions[self._missions_num] = mission
        self._run_or_wait(mission)
        # sleep(.1)
        return mission

    def _run_or_wait(self, mission: Mission):
        """接收任务，有空线程则运行，没有则进入等待队列"""
        thread_id = self._get_usable_thread()
        if thread_id is not None:
            thread = Thread(target=self._run, args=(thread_id, mission))
            self._threads[thread_id] = {'thread': thread, 'mission': None}
            thread.start()
        else:
            self._waiting_list.put(mission)

    def _run(self, ID: int, mission: Mission) -> None:
        """
        :param ID: 线程id
        :param mission: 任务对象，Mission或Task
        :return:
        """
        while True:
            if not mission:  # 如果没有任务，就从等候列表中取一个
                if not self._waiting_list.empty():
                    try:
                        mission = self._waiting_list.get(True, .5)
                    except Exception:
                        self._waiting_list.task_done()
                        break
                else:
                    break

            self._threads[ID]['mission'] = mission
            self._download(mission, ID)
            mission = None

        self._threads[ID] = None

    def get_mission(self, mission_or_id: Union[int, Mission]) -> Mission:
        """根据id值获取一个任务                 \n
        :param mission_or_id: 任务或任务id
        :return: 任务对象
        """
        return self._missions[mission_or_id] if isinstance(mission_or_id, int) else mission_or_id

    def get_failed_missions(self, save_to: Union[str, Path] = None) -> list:
        lst = [i for i in self._missions.values() if i.result is False]
        if save_to:
            lst = [{'url': i.data['file_url'],
                    'path': i.data['goal_path'],
                    'rename': i.data['rename'],
                    'post_data': i.data['post_data'],
                    'kwargs': i.data['kwargs']}
                   for i in lst]
            r = Recorder(save_to, cache_size=0)
            r.add_data(lst)
            r.record()
        return lst

    def wait(self,
             mission: Union[int, Mission] = None,
             show: bool = True,
             timeout: float = None) -> Union[tuple, None]:
        """等待所有或指定任务完成                                    \n
        :param mission: 任务对象或任务id，为None时等待所有任务结束
        :param show: 是否显示进度
        :param timeout: 超时时间，默认为连接超时时间，0为无限
        :return: 任务结果和信息组成的tuple
        """
        timeout = timeout if timeout is not None else self.timeout
        if mission:
            return self.get_mission(mission).wait(show, timeout)

        else:
            if show:
                self.show(False)
            else:
                t1 = perf_counter()
                while self.is_running() or (perf_counter() - t1 < timeout or timeout == 0):
                    sleep(0.1)

    def cancel(self):
        """取消所有等待中或执行中的任务"""
        for m in self._missions.values():
            m.cancel(False)

    def show(self, asyn: bool = True, keep: bool = False) -> None:
        """实时显示所有线程进度                 \n
        :param asyn: 是否以异步方式显示
        :param keep: 任务列表为空时是否保持显示
        :return: None
        """
        if asyn:
            Thread(target=self._show, args=(2, keep)).start()
        else:
            self._show(0.1, keep)

    def _show(self, wait: float, keep: bool = False) -> None:
        """实时显示所有线程进度"""
        self._stop_printing = False

        if keep:
            Thread(target=self._stop_show).start()

        t1 = perf_counter()
        while not self._stop_printing and (keep or self.is_running() or perf_counter() - t1 < wait):
            print(f'\033[K', end='')
            print(f'等待任务数：{self._waiting_list.qsize()}')
            for k, v in self._threads.items():
                m = v['mission'] if v else None
                if m:
                    rate = m.parent.rate if isinstance(m, Task) else m.rate if m else ''
                    path = f'M{m.mid} {rate}% {m}'
                else:
                    path = '空闲'
                print(f'\033[K', end='')
                print(f'线程{k}：{path}')

            print(f'\033[{self.roads + 1}A\r', end='')
            sleep(0.4)

        print(f'\033[1B', end='')
        for i in range(self.roads):
            print(f'\033[K', end='')
            print(f'线程{i}：空闲')

        print()

    def _download(self, mission: Mission, thread_id: int) -> None:
        """此方法是执行下载的线程方法，用于根据任务下载文件     \n
        :param mission: 下载任务对象
        :param thread_id: 线程号
        :return: None
        """
        if mission.state in ('cancel', 'done'):
            mission.state = 'done'
            return

        file_url = mission.data['file_url']
        session: Session = mission.data['session']
        post_data = mission.data['post_data']
        kwargs = mission.data['kwargs']

        if isinstance(mission, Task):
            kwargs = CaseInsensitiveDict(kwargs)
            if 'headers' not in kwargs:
                kwargs['headers'] = {'Range': f"bytes={mission.range[0]}-{mission.range[1]}"}
            else:
                kwargs['headers']['Range'] = f"bytes={mission.range[0]}-{mission.range[1]}"

            mode = 'post' if post_data is not None or kwargs.get('json', None) else 'get'
            # with self._lock:
            #     r, inf = self._make_response(file_url, session=session, mode=mode, data=post_data, **kwargs)
            r, inf = self._make_response(file_url, session=session, mode=mode, data=post_data, **kwargs)
            if r:
                _do_download(r, mission, False, self._lock)
            else:
                _set_result(mission, False, inf, 'done')
                mission.parent.cancel()
                _set_result(mission.parent, False, inf, 'done')

            return

        # ===================开始处理mission====================
        mission.info = '下载中'
        mission.state = 'running'

        rename = mission.data['rename']
        goal_path = mission.data['goal_path']
        file_exists = mission.data['file_exists']
        split = mission.data['split']

        goal_Path = Path(goal_path)
        # 按windows规则去除路径中的非法字符
        goal_path = goal_Path.anchor + sub(r'[*:|<>?"]', '', goal_path.lstrip(goal_Path.anchor)).strip()
        goal_Path = Path(goal_path).absolute()
        goal_Path.mkdir(parents=True, exist_ok=True)
        goal_path = str(goal_Path)

        if file_exists == 'skip' and rename and (goal_Path / rename).exists():
            mission.file_name = rename
            mission.path = goal_Path / rename
            _set_result(mission, 'skip', str(mission.path), 'done')
            return

        mode = 'post' if post_data is not None or kwargs.get('json', None) else 'get'
        # with self._lock:
        #     r, inf = self._make_response(file_url, session=session, mode=mode, data=post_data, **kwargs)
        r, inf = self._make_response(file_url, session=session, mode=mode, data=post_data, **kwargs)
        if not r:
            mission.cancel()
            _set_result(mission, False, inf, 'done')
            return

        # -------------------获取文件信息-------------------
        file_info = _get_file_info(r, goal_path, rename, file_exists, self._lock)
        file_size = file_info['size']
        full_path = file_info['path']
        mission.path = full_path
        mission.file_name = full_path.name
        mission.size = file_size

        if file_info['skip']:
            _set_result(mission, 'skip', full_path, 'done')
            return

        if not r:
            _set_result(mission, False, inf, 'done')
            return

        # -------------------设置分块任务-------------------
        first = False
        if split and file_size and file_size > self.block_size and r.headers.get('Accept-Ranges') == 'bytes':
            first = True
            chunks = [[s, min(s + self.block_size, file_size)] for s in range(0, file_size, self.block_size)]
            chunks[-1][-1] = ''
            chunks_len = len(chunks)

            task1 = Task(mission, chunks[0], f'1/{chunks_len}')
            mission.tasks = []
            mission.tasks.append(task1)

            for ind, chunk in enumerate(chunks[1:], 2):
                task = Task(mission, chunk, f'{ind}/{chunks_len}')
                mission.tasks.append(task)
                self._run_or_wait(task)

        else:  # 不分块
            task1 = Task(mission, None, '1/1')
            mission.tasks.append(task1)

        self._threads[thread_id]['mission'] = task1
        _do_download(r, task1, first, self._lock)

    def _make_response(self,
                       url: str,
                       session: Session,
                       mode: str = 'get',
                       data: Union[dict, str] = None,
                       **kwargs) -> tuple:
        """生成response对象                                                   \n
        :param url: 目标url
        :param mode: 'get', 'post' 中选择
        :param data: post方式要提交的数据
        :param kwargs: 连接参数
        :return: tuple，第一位为Response或None，第二位为出错信息或'Success'
        """
        url = quote(url, safe='/:&?=%;#@+!')
        kwargs = CaseInsensitiveDict(kwargs)
        session = copy_session(session)

        if 'headers' not in kwargs:
            kwargs['headers'] = {}
        else:
            kwargs['headers'] = CaseInsensitiveDict(kwargs['headers'])

        # 设置referer、host和timeout值
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
        scheme = parsed_url.scheme

        if not ('Referer' in kwargs['headers'] or 'Referer' in self.session.headers):
            kwargs['headers']['Referer'] = self._page.url if self._page is not None else f'{scheme}://{hostname}'
        if 'Host' not in kwargs['headers']:
            kwargs['headers']['Host'] = hostname
        if not ('timeout' in kwargs['headers'] or 'timeout' in self.session.headers):
            kwargs['timeout'] = self.timeout

        # 执行连接
        r = err = None
        for i in range(self.retry + 1):
            try:
                if mode == 'get':
                    r = session.get(url, **kwargs)
                elif mode == 'post':
                    r = session.post(url, data=data, **kwargs)

                if r:
                    return _set_charset(r), 'Success'

            except Exception as e:
                err = e

            if r and r.status_code in (403, 404):
                break

            if i < self.retry:
                sleep(self.interval)

        # 返回失败结果
        if r is None:
            return None, '连接失败' if err is None else err
        if not r.ok:
            return r, f'状态码：{r.status_code}'

    def _get_usable_thread(self) -> Union[int, None]:
        """获取可用线程，没有则返回None"""
        for k, v in self._threads.items():
            if v is None:
                return k

    def _stop_show(self):
        input()
        self._stop_printing = True


def _do_download(r: Response, task: Task, first: bool = False, lock: Lock = None):
    """执行下载任务                                    \n
    :param r: Response对象
    :param task: 任务
    :param first: 是否第一个分块
    :param lock: 线程锁
    :return: None
    """
    if task.state in ('cancel', 'done'):
        task.state = 'done'
        return

    task.state = 'running'
    task.info = '下载中'

    while True:  # 争夺文件读写权限
        try:
            f = open(task.path, 'rb+')
            break
        except PermissionError:
            sleep(.2)

    try:
        if first:  # 分块时第一块
            # f.write(next(r.iter_content(chunk_size=task.range[1])))
            blocks = int(task.range[1] / 65536)
            remainder = task.range[1] % 65536
            r_content = r.iter_content(chunk_size=65536)
            for _ in range(blocks):
                if task.state in ('cancel', 'done'):
                    break
                f.write(next(r_content))

            if remainder and task.state not in ('cancel', 'done'):
                f.write(next(r_content)[:remainder])

        else:
            if task.range:
                f.seek(task.range[0])
            for chunk in r.iter_content(chunk_size=65536):
                if task.state in ('cancel', 'done'):
                    break
                if chunk:
                    f.write(chunk)

    except Exception as e:
        success, info = False, f'下载失败。{r.status_code} {e}'

    else:
        success, info = 'success', str(task.path)

    finally:
        f.close()
        r.close()

    task.state = 'done'
    task.result = success
    task.info = info
    mission = task.parent

    if not success:
        mission.cancel()
        mission.result = success
        mission.info = info

    if mission.is_done() and mission.is_success() is False:
        with lock:
            mission.del_file()


def _set_result(mission, res, info, state):
    mission.result = res
    mission.info = info
    mission.state = state
