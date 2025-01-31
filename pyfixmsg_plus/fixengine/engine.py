import asyncio
import logging
from datetime import datetime
from pyfixmsg.codecs.stringfix import Codec
from heartbeat import Heartbeat
from testrequest import TestRequest
from network import Acceptor, Initiator
from fixmessage_factory import FixMessageFactory
from configmanager import ConfigManager
from event_notifier import EventNotifier
from message_handler import (
    MessageProcessor, 
    LogonHandler, 
    TestRequestHandler,  
    ExecutionReportHandler, 
    NewOrderHandler, 
    CancelOrderHandler,
    OrderCancelReplaceHandler,
    OrderCancelRejectHandler,
    NewOrderMultilegHandler,
    MultilegOrderCancelReplaceHandler,
    ResendRequestHandler,
    SequenceResetHandler,
    RejectHandler,
    LogoutHandler
)
from message_store_factory import MessageStoreFactory

class FixEngine:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.is_connected = False
        self.host = self.config_manager.get('FIX', 'host', '127.0.0.1')
        self.port = int(self.config_manager.get('FIX', 'port', '5000'))
        self.sender = self.config_manager.get('FIX', 'sender', 'SENDER')
        self.target = self.config_manager.get('FIX', 'target', 'TARGET')
        self.version = self.config_manager.get('FIX', 'version', 'FIX.4.4')
        self.use_tls = self.config_manager.get('FIX', 'use_tls', 'false').lower() == 'true'
        self.mode = self.config_manager.get('FIX', 'mode', 'initiator').lower()
        db_path = self.config_manager.get('FIX', 'state_file', 'fix_state.db')
        
        self.codec = Codec()
        self.running = False
        self.logger = logging.getLogger('FixEngine')
        self.logger.setLevel(logging.DEBUG)
        self.heartbeat_interval = int(self.config_manager.get('FIX', 'heartbeat_interval', '30'))
        self.message_store = MessageStoreFactory.get_message_store('database', db_path)
        self.message_store.beginstring = self.version
        self.message_store.sendercompid = self.sender
        self.message_store.targetcompid = self.target
        self.response_message = FixMessageFactory.create_message('0')
        self.received_message = FixMessageFactory.create_message('0')
        self.lock = asyncio.Lock()
        self.heartbeat = Heartbeat(self.send_message, self.config_manager, self.heartbeat_interval)
        self.test_request = TestRequest(self.send_message, self.config_manager)
        self.last_heartbeat_time = None
        self.missed_heartbeats = 0
        self.session_id = f"{self.host}:{self.port}"
        self.network = Acceptor(self.host, self.port, self.use_tls) if self.mode == 'acceptor' else Initiator(self.host, self.port, self.use_tls)
        
        self.event_notifier = EventNotifier()
        self.message_processor = MessageProcessor(self.message_store)
        
        # Register message handlers
        self.message_processor.register_handler('A', LogonHandler(self.message_store))
        self.message_processor.register_handler('1', TestRequestHandler(self.message_store))
        self.message_processor.register_handler('8', ExecutionReportHandler(self.message_store))
        self.message_processor.register_handler('D', NewOrderHandler(self.message_store))
        self.message_processor.register_handler('F', CancelOrderHandler(self.message_store))
        self.message_processor.register_handler('G', OrderCancelReplaceHandler(self.message_store))
        self.message_processor.register_handler('9', OrderCancelRejectHandler(self.message_store))
        self.message_processor.register_handler('AB', NewOrderMultilegHandler(self.message_store))
        self.message_processor.register_handler('AC', MultilegOrderCancelReplaceHandler(self.message_store))
        self.message_processor.register_handler('2', ResendRequestHandler(self.message_store))
        self.message_processor.register_handler('4', SequenceResetHandler(self.message_store))
        self.message_processor.register_handler('3', RejectHandler(self.message_store))
        self.message_processor.register_handler('5', LogoutHandler(self.message_store))

    async def connect(self):
        try:
            if self.mode == 'acceptor':
                self.logger.info("Starting in acceptor mode, waiting for incoming connections...")
                await self.network.start_accepting(self.handle_incoming_connection)
            else:
                await self.network.connect()
                self.is_connected = True
                self.logger.info("Connected to FIX server.")
                await self.logon()
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")

    async def handle_incoming_connection(self, reader, writer):
        try:
            self.is_connected = True
            self.logger.info("Accepted incoming connection.")
            self.network.set_transport(reader, writer)
            await self.logon()
            await self.receive_message()
        except Exception as e:
            self.logger.error(f"Error handling incoming connection: {e}")
            self.is_connected = False
            writer.close()
            await writer.wait_closed()

    async def logon(self):
        if not self.is_connected:
            self.logger.error("Cannot logon: not connected.")
            return
        try:
            logon_message = FixMessageFactory.create_message('A')
            logon_message[49] = self.sender
            logon_message[56] = self.target
            logon_message[34] = self.message_store.get_next_outgoing_sequence_number()
            await self.send_message(logon_message)
            await self.heartbeat.start()
        except Exception as e:
            self.logger.error(f"Failed to logon: {e}")

    async def send_message(self, message):
        fix_message = FixMessageFactory.create_message_from_dict(message)
        if not fix_message.anywhere(52):
            fix_message[52] = datetime.utcnow().strftime("%Y%m%d-%H:%M:%S.%f")[:-3]
        fix_message[34] = self.message_store.get_next_outgoing_sequence_number()
        wire_message = fix_message.to_wire(codec=self.codec)
        await self.network.send(wire_message)
        self.message_store.store_message(self.version, self.sender, self.target, fix_message[34], wire_message)
        FixMessageFactory.return_message(fix_message)

    async def receive_message(self):
        try:
            while self.is_connected:
                data = await self.network.receive()
                await self.handle_message(data)
        except Exception as e:
            self.logger.error(f"Error receiving message: {e}")
            self.is_connected = False
            await self.disconnect()

    async def send_reject_message(self, message):
        reject_message = FixMessageFactory.create_message('3')
        reject_message[49] = self.sender
        reject_message[56] = self.target
        reject_message[34] = self.message_store.get_next_outgoing_sequence_number()
        reject_message[45] = message.get(34)
        reject_message[58] = "Invalid checksum"
        await self.send_message(reject_message)

    async def handle_message(self, data):
        async with self.lock:
            self.received_message.clear()
            self.received_message.from_wire(data, codec=self.codec)
            self.logger.info(f"Received: {self.received_message}")

            self.message_store.store_message(self.version, self.sender, self.target, self.received_message[34], data)

            if self.received_message.checksum() != self.received_message[10]:
                self.logger.error("Checksum validation failed for received message.")
                await self.send_reject_message(self.received_message)
                return

            await self.message_processor.process_message(self.received_message)
            msg_type = self.received_message.get(35)

            if msg_type == '0':  # Heartbeat
                self.heartbeat.last_received_time = asyncio.get_event_loop().time()
                if '112' in self.received_message:
                    self.heartbeat.test_request_id = None
            elif msg_type == '1':  # Test Request
                await self.heartbeat.receive_test_request(self.received_message)
            else:
                self.event_notifier.notify(msg_type, self.received_message)

    async def reset_sequence_numbers(self):
        self.message_store.reset_sequence_numbers()
        self.logger.info("Sequence numbers reset to 1 for both inbound and outbound.")
    
    async def set_inbound_sequence_number(self, seq_num):
        self.message_store.set_next_incoming_sequence_number(seq_num)
        self.logger.info(f"Inbound sequence number set to {seq_num}")

    async def set_outbound_sequence_number(self, seq_num):
        self.message_store.set_next_outgoing_sequence_number(seq_num)
        self.logger.info(f"Outbound sequence number set to {seq_num}")

# Example usage
if __name__ == "__main__":
    config_manager = ConfigManager()
    engine = FixEngine(config_manager)
    asyncio.run(engine.connect())
    # Example of setting sequence numbers
    asyncio.run(engine.set_inbound_sequence_number(100))
    asyncio.run(engine.set_outbound_sequence_number(200))
