class TwinoHeaders:
    CLIENT_ID = "Client-Id"
    CLIENT_TOKEN = "Client-Token"
    CLIENT_NAME = "Client-Name"
    CLIENT_TYPE = "Client-Type"
    CLIENT_ACCEPT = "Client-Accept"
    NEGATIVE_ACKNOWLEDGE_REASON = "Nack-Reason"
    REASON = "Reason"
    NACK_REASON_NONE = "none"
    NACK_REASON_ERROR = "error"
    NACK_REASON_NO_CONSUMERS = "no-consumers"
    NACK_REASON_TIMEOUT = "timeout"
    TWINO_MQ_SERVER = "Twino-MQ-Server"
    CHANNEL_NAME = "Channel-Name"
    CC = "CC"
    REQUEST_ID = "Request-Id"
    QUEUE_ID = "Queue-Id"
    NO_CONTENT = "No-Content"
    EMPTY = "Empty"
    UNAUTHORIZED = "Unauthorized"
    UNACCEPTABLE = "Unacceptable"
    NO_CHANNEL = "No-Channel"
    NO_QUEUE = "No-Queue"
    ID_REQUIRED = "Id-Required"
    END = "End"
    ERROR = "Error"
    INDEX = "Index"
    COUNT = "Count"
    ORDER = "Order"
    CLEAR = "Clear"
    INFO = "Info"
    LIFO = "LIFO"
    PRIORITY_MESSAGES = "Priority-Messages"
    MESSAGES = "Messages"
    DELIVERY_HANDLER = "Delivery-Handler"
    WAIT_FOR_ACKNOWLEDGE = "Wait-For-Acknowledge"
    QUEUE_STATUS = "Queue-Status"

    @staticmethod
    def create_line(key: str, value: str) -> str:
        return key + ":" + value + "\r\n"
