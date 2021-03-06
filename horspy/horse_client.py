import asyncio
import ssl
import threading
import socket
import time
import unique_generator

from datetime import timedelta, datetime
from typing import List, Callable, Dict
from message_tracker import MessageTracker
from message_type import MessageType
from protocol_reader import ProtocolReader
from protocol_writer import ProtocolWriter
from pull_container import PullContainer, PullProcess
from pull_request import PullRequest, ClearDecision, MessageOrder
from subscription import Subscription
from result_code import ResultCode
from horse_headers import HorseHeaders
from horse_message import HorseMessage, MessageHeader
from horse_result import HorseResult
from known_content_types import KnownContentTypes


class HorseClient:
    """ Horse Client object """

    # region Properties

    auto_reconnect: bool = True
    """ If true, reconnects automatically when disconnected """

    reconnect_delay: timedelta = timedelta(milliseconds=1500)
    """
    When auto_reconnect is true, client reconnects when disconnected.
    That value is the delay before reconnect attempt.
    """

    ack_timeout: timedelta = timedelta(seconds=5)
    """
    Timeout duration for acknowledge messages of sent messages.
    Default value is 5 secs
    """

    request_timeout: timedelta = timedelta(seconds=15)
    """
    Timeout duration for request messages.
    Default value is 15 secs
    """

    id: str = None
    """
    Unique client id for MQ server.
    If server has another active client with same id, it will generate new id for the client.    
    """

    name: str = "noname"
    """ Client name """

    type: str = "notype"
    """ Client type """

    token: str = None
    """ Client token for server authentication and authorization """

    headers: List[MessageHeader] = []
    """ Handshake message headers """

    ping_interval: timedelta = timedelta(seconds=150)
    """ PING interval """

    smart_heartbeat: bool = True
    """ If true, PING is sent only on idle mode. If there is active traffic, it's skipped. """

    message_received: Callable[[HorseMessage], None] = None
    """ General message received event callback. Handles queue and direct messages. """

    @property
    def connected(self) -> bool:
        return self.__connected

    __socket: socket = None
    __connected: bool = False
    __is_ssl: bool = False
    __read_thread: threading.Thread
    __heartbeat_timer: threading.Timer = None
    __last_ping: datetime = datetime.utcnow()
    __last_receive: datetime = datetime.utcnow()
    __pong_pending: bool = False
    __pong_deadline: datetime
    __tracker: MessageTracker
    __joined_channels: List[str] = []
    __subscriptions: List[Subscription] = []
    __pull_containers: Dict[str, PullContainer] = []

    __ping_bytes = b'\x89\xff\x00\x00\x00\x00\x00\x00'
    __pong_bytes = b'\x8a\xff\x00\x00\x00\x00\x00\x00'

    # endregion

    # region Connection

    def __init__(self):
        self.id = unique_generator.create()
        self.__tracker = MessageTracker()
        self.__tracker.run()

    def destroy(self):
        """ Destroys client, stops all background processes and releases all resources """
        self.__tracker.destroy()
        self.disconnect()

    def __resolve_host(self, host: str) -> (str, int, bool):
        """ Resolves host, protocol and port from full endpoint string """

        sp_protocol = host.split('://')
        sp_host = sp_protocol[0]
        if len(sp_protocol) > 1:
            sp_host = sp_protocol[1]

        sport = sp_host.split(':')
        hostname = sport[0]

        port = 2622
        if len(sport) > 1:
            port_str = sport[1]
            if port_str.endswith('/'):
                port_str = port_str[0:len(port_str) - 1]

            port = int(port_str)

        ssl = False
        if len(sp_protocol) > 1:
            proto = sp_protocol[0].lower().strip()
            if proto == 'hmqs':
                ssl = True

        return (hostname, port, ssl)

    def connect(self, host: str) -> bool:
        """
        Connects to a horse messaging queue host
        :param host: Example hostnames: hmq://127.0.0.1:1234, hmq://localhost:234, hmqs://secure-host.com:555
        :return:
        """

        try:
            self.disconnect()
            resolved = self.__resolve_host(host)
            self.__is_ssl = resolved[2]
            self.__socket = socket.create_connection((resolved[0], resolved[1]))

            if self.__is_ssl:
                context = ssl.create_default_context()
                ssl_socket = context.wrap_socket(self.__socket, server_hostname=resolved[0])
                self.__socket = ssl_socket

            hs = self.__handshake()
            if not hs:
                self.__connected = False
                return False

            self.__connected = True
            self.__init_connection()
            self.__rejoin()
            self.__read_thread = threading.Thread(target=self.__read)
            self.__read_thread.start()

            return True

        except:
            self.__connected = False
            return False

    def __handshake(self) -> bool:
        """
        Sends HMQP handshake message and reads handshake response.
        :returns true if handshake is successful
        """

        self.__socket.sendall("HMQP/2.1".encode('UTF-8'))

        # create handshake message properties
        content = 'CONNECT /\r\n'
        if self.id:
            content += HorseHeaders.create_line(HorseHeaders.CLIENT_ID, self.id)
        if self.name:
            content += HorseHeaders.create_line(HorseHeaders.CLIENT_NAME, self.name)
        if self.type:
            content += HorseHeaders.create_line(HorseHeaders.CLIENT_TYPE, self.type)
        if self.token:
            content += HorseHeaders.create_line(HorseHeaders.CLIENT_TOKEN, self.token)

        if self.headers:
            for h in self.headers:
                content += HorseHeaders.create_line(h.key, h.value)

        msg = HorseMessage()
        msg.type = MessageType.Server
        msg.content_type = KnownContentTypes.HELLO.value
        msg.set_content(content)
        sent = self.send(msg)
        if not sent:
            return False

        hr_result = self.__read_certain(8)
        if hr_result is None:
            return False

        hs_response = hr_result.decode('UTF-8')
        return hs_response == "HMQP/2.1"

    def disconnect(self) -> None:
        """ Disconnects from Horse messaging queue server """
        self.__pong_pending = False
        self.__connected = False

        if self.__heartbeat_timer != None:
            self.__heartbeat_timer.cancel()
            self.__heartbeat_timer = None

        if self.__socket is not None:
            try:
                self.__socket.shutdown(0)
                self.__socket.close()
                self.__socket = None
            except:
                self.__socket = None

    def __pong(self):
        """ Sends pong message as ping response """
        try:
            self.__socket.sendall(self.__pong_bytes)
        except:
            self.disconnect()

    def __heartbeat(self):
        """ Checks client activity and sends PING if required """

        # we are on next heartbeat and previous pending pong still not received
        # connection will be reset
        if self.__pong_pending:
            self.disconnect()
            return

        now = datetime.utcnow()
        diff: timedelta
        if self.smart_heartbeat:
            if self.__last_receive > self.__last_ping:
                diff = now - self.__last_receive
            else:
                diff = now - self.__last_ping
        else:
            diff = now - self.__last_ping

        if diff > self.ping_interval:
            self.__pong_pending = True
            self.__last_ping = datetime.utcnow()
            try:
                self.__socket.sendall(self.__ping_bytes)
            except:
                self.disconnect()
                return

        self.__heartbeat_timer = threading.Timer(self.ping_interval.total_seconds(), self.__heartbeat)
        self.__heartbeat_timer.start()

    def __init_connection(self):
        """ Initializes connection management objects """
        self.__last_ping = datetime.utcnow()
        self.__last_receive = datetime.utcnow()
        self.__pong_pending = False

        if not self.__heartbeat_timer:
            self.__heartbeat_timer = threading.Timer(self.ping_interval.total_seconds(), self.__heartbeat)
            self.__heartbeat_timer.start()

    # endregion

    # region Read

    def __read_certain(self, length: int) -> bytearray:
        """
        Reads a certaion amount of bytes
        """

        left = length
        buf = bytearray(length)
        view = memoryview(buf)
        while left:
            read_count = self.__socket.recv_into(view, left)
            if read_count == 0:
                return None

            view = view[read_count:]  # slicing views is cheap
            left -= read_count

        return buf

    def __read(self):
        """ Reads messages from socket while connected """

        reader = ProtocolReader()
        while True:
            try:
                if not self.__connected:
                    return

                message = reader.read(self.__socket)
                if message is None:
                    self.disconnect()
                    return

                self.__last_receive = datetime.utcnow()

                if message.type == MessageType.Terminate.value:
                    self.disconnect()

                elif message.type == MessageType.Ping.value:
                    self.__pong()

                elif message.type == MessageType.Pong.value:
                    self.__pong_pending = False

                elif message.type == MessageType.Server.value:
                    if message.content_type == KnownContentTypes.ACCEPTED:
                        self.id = message.target

                # handle queue message
                elif message.type == MessageType.QueueMessage.value:

                    # check if pull request's response message
                    if len(self.__pull_containers) > 0 and message.has_header:
                        request_id = message.get_header(HorseHeaders.REQUEST_ID)
                        if request_id in self.__pull_containers:
                            pull_container = self.__pull_containers[request_id]

                            # message received
                            if message.length > 0:
                                pull_container.received_count += 1
                                pull_container.messages.append(message)
                                if pull_container.each_msg_func:
                                    pull_container.each_msg_func(pull_container.received_count, message)

                            # end of pull request
                            else:
                                no_content = message.get_header(HorseHeaders.NO_CONTENT)
                                if no_content:
                                    self.__pull_containers.pop(pull_container.request_id)
                                    try: # already completed future check (maybe timed out at same time etc)
                                        pull_container.future.set_result(None)
                                    except:
                                        pass

                    # find subscriptions
                    queue_subs = next((x for x in self.__subscriptions
                                       if not x.direct and x.channel == message.target
                                       and x.content_type == message.content_type), None)
                    if queue_subs:
                        for act in queue_subs.actions:
                            act(message)

                    # trigger general event
                    if not self.message_received is None:
                        self.message_received(message)

                # handle direct message
                elif message.type == MessageType.DirectMessage.value:
                    # find subscriptions
                    direct_subs = next((x for x in self.__subscriptions
                                        if x.direct and x.content_type == message.content_type), None)
                    if direct_subs:
                        for act in direct_subs.actions:
                            act(message)

                    # trigger general event
                    if not self.message_received is None:
                        self.message_received(message)

                elif message.type == MessageType.Acknowledge.value or message.type == MessageType.Response.value:
                    asyncio.run(self.__tracker.process(message))

                # if message.type == MessageType.Event.value:
                #    pass

            except:
                self.__read_thread = None
                if self.__connected and self.__socket.fileno() == -1:
                    self.disconnect()
                    return

    # endregion

    # region Subscription

    async def on(self, channel: str, queue: int, func: Callable[[HorseMessage], None], auto_join: bool = True):
        """
        Subscribes to a queue in a channel
        :param channel: Channel name
        :param queue: Queue Id
        :param func: Function that will be called when a message is received
        :param auto_join: If true and client still not joined to channel, joins
        :return:
        """

        subs = next((x for x in self.__subscriptions
                     if not x.direct and x.channel == channel and x.content_type == queue), None)

        if not subs:
            subs = Subscription()
            subs.channel = channel
            subs.content_type = queue
            subs.direct = False
            self.__subscriptions.append(subs)

        subs.actions.append(func)

        if auto_join:
            joined = next((x for x in self.__joined_channels if x == channel), None)
            if not joined:
                await self.join(channel)

    def off(self, channel: str, queue: int, auto_leave: bool = False):
        """
        Unsubscribes from a queue in a channel
        :param channel: Channel name
        :param queue: Queue Id
        :param auto_leave: If true, client leaves from channel to. If you have multiple queues in same channel, leaving from channel affects other subscriptions.
        :return:
        """

        subs = next(
            x for x in self.__subscriptions if not x.direct and x.channel == channel and x.content_type == queue)
        if subs:
            self.__subscriptions.remove(subs)

        if auto_leave:
            joined = next((x for x in self.__joined_channels if x == channel), None)
            if joined:
                self.leave(channel)

    def on_direct(self, content_type: int, func: Callable[[HorseMessage], None]):
        """
        Subscribes to all direct messages with specified content type
        :param content_type: Message content type
        :param func: Function that will be called when a message is received
        :return:
        """

        subs = next((x for x in self.__subscriptions if x.direct and x.content_type == content_type), None)
        if not subs:
            subs = Subscription()
            subs.channel = None
            subs.content_type = content_type
            subs.direct = True
            self.__subscriptions.append(subs)

        subs.actions.append(func)

    def off_direct(self, content_type: int):
        """
        Unsubscribes from all direct messages with specified content type
        :param content_type: Message content type
        :return:
        """

        subs = next((x for x in self.__subscriptions if x.direct and x.content_type == content_type), None)
        if subs:
            self.__subscriptions.remove(subs)

    # endregion

    # region Channels

    async def join(self, channel: str, wait_ack: bool = False) -> HorseResult:
        """
        Joins to a channel
        :param channel: Channel name
        :param wait_ack: If true, waits for acknowledge from server
        :return: If waits for ack, ack result. Otherview Ok if message is sent successfuly
        """

        msg = HorseMessage()
        msg.type = MessageType.Server
        msg.content_type = KnownContentTypes.JOIN.value
        msg.target = channel
        msg.pending_response = wait_ack

        result: HorseResult
        if wait_ack:
            msg.message_id = unique_generator.create()
            result = await self.request(msg)
        else:
            result = self.send(msg)

        # add channel to joined list (if not already added)
        if result.code == ResultCode.Ok:
            has = next((x for x in self.__joined_channels if x == channel), None)
            if not has:
                self.__joined_channels.append(channel)

        return result

    async def leave(self, channel: str, wait_ack: bool = False) -> HorseResult:
        """
        Leavess from a channel
        :param channel: Channel name
        :param wait_ack: If true, waits for acknowledge from server
        :return: If waits for ack, ack result. Otherview Ok if message is sent successfuly
        """

        msg = HorseMessage()
        msg.type = MessageType.Server
        msg.content_type = KnownContentTypes.LEAVE.value
        msg.target = channel
        msg.pending_response = wait_ack

        result: HorseResult
        if wait_ack:
            msg.message_id = unique_generator.create()
            result = await self.request(msg)
        else:
            result = self.send(msg)

        # remove channel from joined channel list
        if result.code == ResultCode.Ok:
            self.__joined_channels.remove(channel)

        return result

    def __rejoin(self):
        """ Rejoins to all channels joined in previous connections """
        if not self.__joined_channels:
            self.__joined_channels = []

        if len(self.__joined_channels) == 0:
            return
        for ch in self.__joined_channels:
            self.join(ch)

    # endregion

    # region Send

    def send(self, msg: HorseMessage, additional_headers: List[MessageHeader] = None) -> HorseResult:
        """
        Sends a raw message to server. Returns true if all data sent over network.
        :param msg: Sending message
        :param additional_headers: Additional message headers
        :return: Successful if the message sent over network. Otherwise returns failed.
        """

        try:
            writer = ProtocolWriter()
            if msg.source_len == 0:
                msg.source = self.id

            if not msg.message_id:
                msg.message_id = unique_generator.create()

            bytes = writer.write(msg, additional_headers)
            self.__socket.sendall(bytes.getbuffer())
            result = HorseResult()
            result.code = ResultCode.Ok
            return result
        except:
            self.disconnect()
            result = HorseResult()
            result.code = ResultCode.Failed
            return result

    async def send_get_ack(self, msg: HorseMessage,
                           additional_headers: List[MessageHeader] = None) -> HorseResult:  # Awaitable[HorseResult]:
        """
        Sends a message and waits for acknowledge
        :param msg: Sending message
        :param additional_headers: Additional message headers
        :return: Returns a result after acknowledge received or timed out
        """

        future: asyncio.Future = None
        try:
            writer = ProtocolWriter()
            if msg.source_len == 0:
                msg.source = self.id

            if not msg.message_id:
                msg.message_id = unique_generator.create()

            msg.pending_response = False
            if not msg.pending_acknowledge:
                msg.pending_acknowledge = True

            tracking = await self.__tracker.track(msg, self.ack_timeout)
            bytes = writer.write(msg, additional_headers)
            self.__socket.sendall(bytes.getbuffer())

            while not tracking.future.done():
                time.sleep(0.001)

            resp: HorseMessage = await tracking.future
            result = HorseResult()
            if resp is None:
                result.code = ResultCode.RequestTimeout
                result.reason = "timeout"
            else:
                nack_value = resp.get_header(HorseHeaders.NEGATIVE_ACKNOWLEDGE_REASON)
                if nack_value is None:
                    result.code = ResultCode.Ok
                    result.reason = ""
                else:
                    result.code = ResultCode.Failed
                    result.reason = nack_value

            return result

        except:
            self.disconnect()
            if future is not None:
                await self.__tracker.forget(msg)

            result = HorseResult()
            result.code = ResultCode.SendError
            result.reason = ""
            return result

    async def request(self, msg: HorseMessage,
                      additional_headers: List[MessageHeader] = None) -> HorseResult:
        """
        Sends a request and waits for response
        :param msg: Request message
        :param additional_headers: Additional headers
        :return: Response message is message variable of Horse result
        """

        future: asyncio.Future = None
        try:
            writer = ProtocolWriter()
            if msg.source_len == 0:
                msg.source = self.id

            msg.pending_acknowledge = False
            if not msg.pending_response:
                msg.pending_response = True

            tracking = await self.__tracker.track(msg, self.request_timeout)
            bytes = writer.write(msg, additional_headers)
            self.__socket.sendall(bytes.getbuffer())

            while not tracking.future.done():
                time.sleep(0.001)

            resp: HorseMessage = await tracking.future
            result = HorseResult()
            if resp is None:
                result.code = ResultCode.RequestTimeout
                result.reason = "timeout"
            else:
                result.code = resp.content_type
                result.message = resp
                result.reason = resp.get_header(HorseHeaders.REASON)

            return result

        except:
            self.disconnect()
            if future is not None:
                await self.__tracker.forget(msg)

            result = HorseResult()
            result.code = ResultCode.SendError
            return result

    async def send_direct(self, target: str,
                          content_type: int,
                          message: str,
                          wait_ack: bool,
                          additional_headers: List[MessageHeader] = None) -> HorseResult:
        """
        Sends a direct message to a receiver
        :param target: Unique Id of the client or @name:name_of_client or @type:type_of_client
        :param content_type: Message content type
        :param message: String message content
        :param wait_ack: If true, message will wait for acknowledge
        :param additional_headers: Additional message headers
        :return:
        """

        msg = HorseMessage()
        msg.type = MessageType.DirectMessage
        msg.content_type = content_type
        msg.target = target

        msg.set_content(message)
        if wait_ack:
            msg.pending_acknowledge = True
            return await self.send_get_ack(msg, additional_headers)
        else:
            msg.pending_acknowledge = False
            return self.send(msg, additional_headers)

    async def push_queue(self, channel: str,
                         queue: int,
                         message: str,
                         wait_ack: bool,
                         additional_headers: List[MessageHeader] = None) -> HorseResult:
        """
        Pushes a message into a queue
        :param channel: Channel name of the queue
        :param queue: Queue Id
        :param message: String message content
        :param wait_ack: If true, waits for acknowledge
        :param additional_headers: Additional message headers
        :return: If operation successful, returns Ok
        """

        msg = HorseMessage()
        msg.type = MessageType.QueueMessage
        msg.content_type = queue
        msg.target = channel

        msg.set_content(message)
        if wait_ack:
            msg.pending_acknowledge = True
            return await self.send_get_ack(msg, additional_headers)
        else:
            msg.pending_acknowledge = False
            return self.send(msg, additional_headers)

    async def publish_router(self, router: str,
                             content_type: int,
                             message: str,
                             wait_ack: bool,
                             additional_headers: List[MessageHeader] = None) -> HorseResult:
        """
        Publishes a message to a router
        :param router: Router name
        :param content_type: Message content type. It's used in router. Real queue content type may be different (if overwritten in server)
        :param message: String message content
        :param wait_ack: If true, waits for acknowledge
        :param additional_headers: Additional message headers
        :return: If operation successful, returns Ok
        """

        msg = HorseMessage()
        msg.type = MessageType.Router
        msg.content_type = router
        msg.target = content_type

        msg.set_content(message)
        if wait_ack:
            msg.pending_acknowledge = True
            return await self.send_get_ack(msg, additional_headers)
        else:
            msg.pending_acknowledge = False
            return self.send(msg, additional_headers)

    def ack(self, message: HorseMessage) -> HorseResult:
        """
        Sends a positive acknowledge
        :param message: Message that will be acknowledged
        :return: Returns Ok if sent successfuly
        """
        return self.__send_ack(message, None)

    def negative_ack(self, message: HorseMessage, reason: str = None) -> HorseResult:
        """
        Sends a negative acknowledge
        :param message: Message that will be acknowledged
        :param reason: Negative acknowledge reason. If None, reason will be "none"
        :return: Returns Ok if sent successfuly
        """

        r = reason
        if not r:
            r = HorseHeaders.NACK_REASON_NONE
        return self.__send_ack(message, r)

    def __send_ack(self, message: HorseMessage, reason: str) -> HorseResult:
        """
        Sends an acknowledge message
        :param message: Message that will be acknowledged
        :param reason: Negative acknowledge reason. If None, acknowledge is positive
        :return: Returns Ok if sent successfuly
        """

        msg = HorseMessage()
        msg.type = MessageType.Acknowledge
        msg.content_type = message.content_type
        msg.message_id = message.message_id
        msg.first_acquirer = message.first_acquirer

        if message.type == MessageType.DirectMessage.value:
            msg.high_priority = True
            msg.source = message.target
            msg.target = message.source
        else:
            msg.high_priority = False
            msg.target = message.target

        if reason:
            msg.add_header(HorseHeaders.NEGATIVE_ACKNOWLEDGE_REASON, reason)

        return self.send(msg)

    def response(self, request_msg: HorseMessage,
                 status: ResultCode,
                 response_content: str = None,
                 additional_headers: List[MessageHeader] = None) -> HorseResult:
        """
        Sends a response message to a request
        :param request_msg: Request message
        :param status: Response message status
        :param response_content: Response message content
        :param additional_headers: Additional message headers
        :return: Returns ok, if sent successfully
        """

        msg = HorseMessage()
        msg.type = MessageType.Response
        msg.content_type = status.value
        msg.message_id = request_msg.message_id
        msg.high_priority = request_msg.high_priority
        msg.first_acquirer = request_msg.first_acquirer

        if request_msg.type == MessageType.QueueMessage.value:
            msg.target = request_msg.target
        else:
            msg.target = request_msg.source

        if not response_content is None:
            msg.set_content(response_content)

        return self.send(msg, additional_headers)

    async def pull(self, request: PullRequest, each_msg_func: Callable[[int, HorseMessage], None]) -> PullContainer:

        msg = HorseMessage()
        msg.type = MessageType.QueuePullRequest
        msg.message_id = unique_generator.create()
        msg.target = request.channel
        msg.content_type = request.queue_id

        msg.add_header(HorseHeaders.COUNT, str(request.count))

        if request.clear_after == ClearDecision.AllMessages.value:
            msg.add_header(HorseHeaders.CLEAR, "all")
        elif request.clear_after == ClearDecision.PriorityMessages.value:
            msg.add_header(HorseHeaders.CLEAR, "High-Priority")
        elif request.clear_after == ClearDecision.Messages.value:
            msg.add_header(HorseHeaders.CLEAR, "Default-Priority")

        if request.get_counts:
            msg.add_header(HorseHeaders.INFO, "yes")

        if request.order == MessageOrder.LIFO.value:
            msg.add_header(HorseHeaders.ORDER, HorseHeaders.LIFO)

        if request.request_headers:
            for header in request.request_headers:
                msg.add_header(header.key, header.value)

        container = PullContainer()
        container.request_id = msg.message_id
        container.request_count = request.count
        container.received_count = 0
        container.status = PullProcess.Receiving
        container.messages = []
        container.each_msg_func = each_msg_func
        container.last_received = datetime.utcnow()
        container.future = asyncio.Future()

        self.__pull_containers[msg.message_id] = container

        send_result = self.send(msg)
        if send_result.code != ResultCode.Ok:
            self.__pull_containers.pop(msg.message_id)
            return container

        while not container.future.done():
            time.sleep(0.001)
            diff = datetime.utcnow() - container.last_received
            if diff > self.request_timeout:
                self.__pull_containers.pop(msg.message_id)
                container.future.set_result(None)
                break

        await container.future
        return container

    # endregion
