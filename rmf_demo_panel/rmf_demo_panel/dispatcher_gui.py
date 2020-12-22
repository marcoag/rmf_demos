
# Copyright 2020 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
The main Interfaces to the front end GUI are:
1) HTTP interfaces are:  /submit_task, /cancel_task, /get_task, /get_robots
2) socketIO broadcast states: /task_status, /robot_states, /ros_time
"""

import sys
import os
import rclpy
import math
import yaml
import argparse
import time
import json
import logging

from threading import Thread

from rclpy.node import Node
from rclpy.time import Time
from rclpy.parameter import Parameter

from rclpy.qos import qos_profile_system_default
from rclpy.qos import QoSProfile

from rmf_task_msgs.srv import SubmitTask, GetTaskList, CancelTask
from rmf_task_msgs.msg import TaskType
from rmf_fleet_msgs.msg import FleetState

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, disconnect

###############################################################################


class DispatcherClient(Node):
    def __init__(self, dashboard_config):
        super().__init__('dispatcher_client')
        self.submit_task_srv = self.create_client(SubmitTask, '/submit_task')
        self.cancel_task_srv = self.create_client(CancelTask, '/cancel_task')
        self.get_task_srv = self.create_client(GetTaskList, '/get_task')

        qos_profile = QoSProfile(depth=10)
        self.dashboard_config = dashboard_config

        # to show robot states
        self.fleet_state_subscription = self.create_subscription(
            FleetState, 'fleet_states', self.fleet_state_cb,
            qos_profile=qos_profile)
        self.fleet_states_dict = {}
        self.tasks_assignments = {}

        # just check one srv endpoint
        while not self.submit_task_srv.wait_for_service(timeout_sec=1.0):
            self.get_logger().warn('Dispatcher node is not avail, waiting...')

    def fleet_state_cb(self, msg: FleetState):
        fleet_name = msg.name
        self.fleet_states_dict[fleet_name] = msg.robots

    def spin_once(self):
        rclpy.spin_once(self, timeout_sec=0.1)

    def ros_time(self) -> int:
        return self.get_clock().now().to_msg().sec

    def submit_task_request(self, req_msg) -> str:
        """
        Task Submission - This function will trigger a ros srv call to the
        dispatcher node, and return a response. Function will return a Task ID
        """
        print("Submit Task Request!")
        try:
            future = self.submit_task_srv.call_async(req_msg)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.4)
            response = future.result()
            if response is None:
                self.get_logger().error('Failed to get srv result !!!')
            else:
                self.get_logger().info(
                    f'New Dispatch task_id {response.task_id}')
                return response.task_id
        except Exception as e:
            self.get_logger().error('Error! Srv call failed !!!  %r' % (e,))
        return "FAILED"

    def cancel_task_request(self, task_id) -> bool:
        """
        Cancel Task - This function will trigger a ros srv call to the
        dispatcher node, and return a response. 
        """
        print("Canceling Task Request!")
        req = CancelTask.Request()
        req.task_id = task_id
        try:
            future = self.cancel_task_srv.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.4)
            response = future.result()
            if response is None:
                self.get_logger().error('Failed to get srv result !!!')
            else:
                self.get_logger().info(
                    f'Cancel Task, success? {response.success}')
                return response.success
        except Exception as e:
            self.get_logger().error('Error! Srv call failed %r' % (e,))
        return False

    # either from DB or via srv call
    def get_task_status(self):
        """
        Get all task status - This fn will trigger a ros srv call to acquire 
        all submitted tasks to dispatcher node. Fn returns an object of tasks
        """
        req = GetTaskList.Request()
        try:
            future = self.get_task_srv.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.4)
            response = future.result()
            if response is None:
                self.get_logger().error('Failed to get srv result !!!')
            else:
                # self.get_logger().info(f'Get Task, success? \
                #   {response.success}')
                active_tasks = self.__convert_task_status_msg(
                    response.active_tasks, False)
                terminated_tasks = self.__convert_task_status_msg(
                    response.terminated_tasks, True)
                self.__generate_assignments_list(active_tasks)
                return active_tasks + terminated_tasks
        except Exception as e:
            self.get_logger().error('Service call failed %r' % (e,))
        return []  # empty list

    def get_robot_states(self):
        """
        This function will return an aggregated list of robot states to the 
        front end UI when a ajax GET is requested.
        """
        agg_robot_states = []
        for fleet_name, robot_states in self.fleet_states_dict.items():
            robots = self.__convert_robot_states_msg(fleet_name, robot_states)
            agg_robot_states = agg_robot_states + robots
        return agg_robot_states

    def __convert_task_status_msg(self, task_summaries, is_done=True):
        """
        convert task summary msg and return a jsonify-able task status obj
        """
        states_enum = {0: "Queued", 1: "Active/Executing", 2: "Completed",
                       3: "Failed", 4: "Canceled", 5: "Pending"}
        type_enum = {0: "Station", 1: "Loop", 2: "Delivery",
                     3: "Charging", 4: "Clean", 5: "Patrol"}

        status_list = []
        rclpy.spin_once(self, timeout_sec=0.0)
        now = self.get_clock().now().to_msg().sec  # only use sec
        for task in task_summaries:
            desc = task.task_profile
            status = {}
            status["task_id"] = desc.task_id
            status["state"] = states_enum[task.state]
            status["done"] = is_done
            status["fleet_name"] = task.fleet_name
            status["robot_name"] = task.robot_name
            status["task_type"] = type_enum[desc.task_type.type]
            status["submited_start_time"] = task.task_profile.start_time.sec
            status["start_time"] = task.start_time.sec     # only use sec
            status["end_time"] = task.end_time.sec         # only use sec

            if status["task_type"] == "Clean":
                status["description"] = desc.clean.start_waypoint
            elif status["task_type"] == "Loop":
                status["description"] = desc.loop.start_name + " --> " + \
                    desc.loop.finish_name + " x" + str(desc.loop.num_loops)
            elif status["task_type"] == "Delivery":
                status["description"] = desc.delivery.pickup_place_name + \
                    " --> " + desc.delivery.dropoff_place_name
            elif status["task_type"] == "Charging":
                status["description"] = "Back to Charging Station"

            # Current hack to generate a progress percentage
            duration = abs(task.end_time.sec - task.start_time.sec)
            if is_done and states_enum[task.state] == "Completed":
                status["progress"] = "100%"
            elif duration == 0 or status["state"] == "Queued":
                status["progress"] = "0%"
            else:
                percent = int(100*(now - task.start_time.sec)/float(duration))
                if (percent < 0):
                    status["progress"] = "queued"
                elif (percent > 100):
                    status["progress"] = "in-progress"
                else:
                    status["progress"] = f"{percent}%"
            status_list.insert(0, status)  # insert front
        return status_list

    def __generate_assignments_list(self, active_task_list):
        """
        Input as active task list, which is in the form of jsonified format
        The assignment here is a format of {bot_name: "string of task IDs"}
        """
        self.tasks_assignments.clear()
        temp_assignments = {}
        for task in active_task_list:
            temp_assignments.setdefault(task["robot_name"], []).append(task)
        for bot_name, tasks in temp_assignments.items():
            # sort with start time
            tasks.sort(key=lambda x: x.get('start_time'))
            string_list = ""
            for task in tasks:
                string_list = string_list + task["task_id"] + "  "
            self.tasks_assignments[bot_name] = string_list

    def __convert_robot_states_msg(self, fleet_name, robot_states):
        """
        convert robot states msg to a jsonify-able robot_states
        """
        bots = []
        mode_enum = {0: "Idle-0", 1: "Charging-1", 2: "Moving-2",
                     3: "Paused-3", 4: "Waiting-4", 5: "Emengency-5",
                     6: "GoingHome-6", 7: "Dock/Clean-7", 8: "AdpterError-8"}
        for bot in robot_states:
            state = {}
            state["robot_name"] = bot.name
            state["fleet_name"] = fleet_name
            state["mode"] = mode_enum[bot.mode.mode]
            state["battery_percent"] = bot.battery_percent
            # time is missing here
            state["location_x"] = bot.location.x
            state["location_y"] = bot.location.y
            state["location_yaw"] = bot.location.yaw
            state["level_name"] = bot.location.level_name

            # task assingments, result is updated from "get_tasks"
            if bot.name in self.tasks_assignments:
                state["assignments"] = self.tasks_assignments[bot.name]
            else:
                state["assignments"] = ""

            bots.append(state)
        return bots

    def convert_task(self, task_json):
        """
        :param obj task_json: 
        :return rmf submit task req_msgs
        This is to convert a json task req format to a rmf_task_msgs
        task_profile format. add this accordingly when a new msg field 
        is introduced.
        The 'start time' here is refered to the "Duration" from now.
        """
        req_msg = SubmitTask.Request()

        if (("task_type" not in task_json) or
            ("start_time" not in task_json) or
                ("description" not in task_json)):
            print("Error!! Key value is invalid!")
            return None

        if ("evaluator" in task_json):
            evaluator = task_json["evaluator"]
            if (evaluator == "lowest_delta_cost"):
                req_msg.evaluator = req_msg.LOWEST_DIFF_COST_EVAL
            elif (evaluator == "lowest_cost"):
                req_msg.evaluator = req_msg.LOWEST_COST_EVAL
            elif (evaluator == "quickest_time"):
                req_msg.evaluator = req_msg.QUICKEST_FINISH_EVAL
            else:
                print("Error!! INVALID evaluator, pls check!")
                return None

        try:
            desc = task_json["description"]
            task_config = self.dashboard_config["task"][task_json["task_type"]]
            if task_json["task_type"] == "Clean":
                req_msg.task_type.type = TaskType.TYPE_CLEAN
                req_msg.clean.start_waypoint = desc["cleaning_zone"]
            elif task_json["task_type"] == "Loop":
                req_msg.task_type.type = TaskType.TYPE_LOOP
                req_msg.loop.num_loops = int(desc["num_loops"])
                req_msg.loop.start_name = desc["start_name"]
                req_msg.loop.finish_name = desc["finish_name"]
            elif task_json["task_type"] == "Delivery":
                req_msg.task_type.type = TaskType.TYPE_DELIVERY
                print(task_config)
                opt = task_config["option"][desc["option"]]
                req_msg.delivery.pickup_place_name = opt["pickup_place_name"]
                req_msg.delivery.pickup_dispenser = opt["pickup_dispenser"]
                req_msg.delivery.dropoff_ingestor = opt["dropoff_ingestor"]
                req_msg.delivery.dropoff_place_name = opt["dropoff_place_name"]
            else:
                print("ERROR! Invalid format")
                return None

            # Calc start time, convert min to sec: TODO better represenation
            rclpy.spin_once(self, timeout_sec=0.0)
            ros_start_time = self.get_clock().now().to_msg()
            ros_start_time.sec += int(task_json["start_time"]*60)
            req_msg.start_time = ros_start_time

        except Exception as e:
            print('Error!! Task Req description is invalid: ', e)
            return None

        return req_msg

###############################################################################


app = Flask(__name__, static_url_path="/static")
socketio = SocketIO(app, async_mode='threading')

# TODO: Layout of frontend gui will depends on this config.json file. This can
# retrived via static file, and configure layout of Adhoc task submisison form.
json_config = {}
with app.open_resource('static/dashboard_config.json') as f:
    contents = f.read()
    json_config = json.loads(contents)

rclpy.init(args=None)
dispatcher_client = DispatcherClient(json_config)

# logging config
logging.getLogger('werkzeug').setLevel(logging.ERROR)  # hide logs from flask
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)s %(message)s',
                    filename='web_server.log',
                    filemode='w')

###############################################################################


@app.route("/")
def home():
    return render_template("index.html")


@app.route('/submit_task', methods=['POST'])
def submit():
    if request.method == "POST":
        logging.debug(f" ROS Time: {dispatcher_client.ros_time()} | \
            Task Submission: {json.dumps(request.json)}")
        req_msg = dispatcher_client.convert_task(request.json)
        return dispatcher_client.submit_task_request(req_msg)
    return ""


@app.route('/cancel_task', methods=['POST'])
def cancel():
    if request.method == "POST":
        cancel_id = request.json['task_id']
        print(cancel_id)
        if (dispatcher_client.cancel_task_request(cancel_id)):
            return " Cancel Success"
    return " Failed to cancel"


# TODO: use socket broadcast.
@app.route('/get_task', methods=['GET'])
def status():
    task_status = jsonify(dispatcher_client.get_task_status())
    logging.debug(f" ROS Time: {dispatcher_client.ros_time()} | \
        Task Status: {json.dumps(task_status.json)}")
    return task_status


# TODO: use socket broadcast
@app.route('/get_robots', methods=['GET'])
def robots():
    robot_status = jsonify(dispatcher_client.get_robot_states())
    logging.debug(f" ROS Time: {dispatcher_client.ros_time()} | \
        Robot Status: {json.dumps(robot_status.json)}")
    return robot_status


###############################################################################

def load_cleaning_tasks(yaml_file):
    try:
        with open(yaml_file, 'r') as stream:
            try:
                print("Loaded Yaml input task")
                load_tasks = yaml.safe_load(stream)
                # TODO: will need to test this
                req_msg = dispatcher_client.convert_task(load_tasks)
                dispatcher_client.submit_task_request(req_msg)
            except yaml.YAMLError as exc:
                print("YAML ERROR",  exc)
    except:
        raise ValueError('ERROR: Unable load Task config file')


def web_server_spin():
    while rclpy.ok():
        dispatcher_client.spin_once()
        time.sleep(0.2)


def broadcast_states():
    """
    Robot_states, tasks_status, and ros_time are being broadcasted
    to frontend UIs via socketIO, periodically (every 2s)
    """
    ns = '/status_updates'
    while rclpy.ok():
        with app.test_request_context():
            tasks = dispatcher_client.get_task_status()
            robots = dispatcher_client.get_robot_states()
            ros_time = dispatcher_client.ros_time()
            socketio.emit('task_status', tasks, broadcast=True, namespace=ns)
            socketio.emit('robot_states', robots, broadcast=True, namespace=ns)
            socketio.emit('ros_time', ros_time, broadcast=True, namespace=ns)

            logging.debug(f" ROS Time: {ros_time} | tasks num: {len(tasks)} \
                active robots: {len(robots)}")
        time.sleep(2)

###############################################################################


def main(args=None):
    parser = argparse.ArgumentParser(
        description='Cleaning Dispatcher GUI Server')
    parser.add_argument("--load", help="preload tasks config yaml path")
    parser.add_argument('args', nargs=argparse.REMAINDER)

    # hackish way to solve rosparam input in launch file
    parser.add_argument("--ros-args", help="ignore this", nargs='?', const='')
    parser.add_argument("--params-file", help="ignore this",
                        nargs='?', const='')
    args = parser.parse_args()
    if args.load:
        print("load task config yaml: ", args.load)
        load_cleaning_tasks(args.load)

    server_ip = "0.0.0.0"

    if "DISPATCHER_GUI_IP_ADDRESS" in os.environ:
        server_ip = os.environ['DISPATCHER_GUI_IP_ADDRESS']
        print(f"set ip to: {server_ip}")

    spin_thread = Thread(target=web_server_spin, args=())
    spin_thread.start()

    broadcast_thread = Thread(target=broadcast_states, args=())
    broadcast_thread.start()

    print("Starting Dispatcher GUI Server")
    app.run(host=server_ip, port=5000, debug=False)
    dispatcher_client.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main(sys.argv)
