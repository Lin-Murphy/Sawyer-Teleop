"""Initialize only a newly created local Gazebo scene, before starting B."""
import time
from sawyer_teleop.neutral import gazebo_neutral_pose, neutral_target, read_neutral_pose
from sawyer_teleop.protocol import JOINTS


def initial_arm_pose(get_param):
    """Use the same validated, simulation-only destination as B's Reset."""
    capability = gazebo_neutral_pose(read_neutral_pose(get_param),
                                     get_param('/robot_description', ''))
    return dict(zip(JOINTS, neutral_target(capability)))


SPAWNER = '/urdf_spawner'


def wait_for_spawner(node_names, timeout=30, sleep=time.sleep, clock=time.monotonic):
    """Wait until gazebo_ros spawn_model has applied the launch -J pose and exited.

    It sleeps ~1 s after spawning before setting the joints (its own race
    workaround). Configuring earlier lets Gazebo map J6 -2.967 to the full-turn
    equivalent nearest the zero pose (+3.316, past the +3.14 limit); the late
    -J call then lands near +4.68 and the wrapped position error holds J6 at the stop.
    """
    end = clock()+timeout
    while SPAWNER in node_names():
        if clock() > end:
            raise RuntimeError('Gazebo model spawner did not finish; B was not started')
        sleep(.1)


def main(node_name='gazebo_scene_initialize'):
    import rospy
    from controller_manager_msgs.srv import ListControllers
    from gazebo_msgs.srv import SetModelConfiguration
    from intera_core_msgs.msg import JointCommand
    from sensor_msgs.msg import JointState
    from std_msgs.msg import Float64
    from std_srvs.srv import Empty

    # The container has its own ROS master and does not expose ROS ports.
    end = time.monotonic() + 100
    while time.monotonic() < end:
        try:
            if rospy.get_param('/use_sim_time', False):
                break
        except Exception:
            pass
        time.sleep(.2)
    else:
        raise RuntimeError('Local Gazebo master did not become ready')
    rospy.init_node(node_name, anonymous=True)
    if not rospy.get_param('/use_sim_time', False):
        raise RuntimeError('Initializer requires simulation time')
    # Validate before touching physics; retain official named-pose parameters.
    home = initial_arm_pose(rospy.get_param)
    rospy.set_param('/sawyer_local_home', home)
    measured = {}
    received = [0.0]
    def feedback(message):
        measured.update(zip(message.name, message.position))
        received[0] = time.monotonic()
    sub = rospy.Subscriber('/robot/joint_states', JointState, feedback, queue_size=1)
    command = rospy.Publisher('/robot/limb/right/joint_command', JointCommand, queue_size=1)
    fingers = [rospy.Publisher('/robot/generic_' + side + '_finger_controller/command',
                               Float64, queue_size=1) for side in ('left', 'right')]
    service = '/robot/controller_manager/list_controllers'
    rospy.wait_for_service(service, timeout=90)
    controllers = rospy.ServiceProxy(service, ListControllers)
    required = {'joint_state_controller', 'right_joint_position_controller',
                'generic_left_finger_controller', 'generic_right_finger_controller'}
    while time.monotonic() < end:
        running = {c.name for c in controllers().controller if c.state == 'running'}
        if required <= running and command.get_num_connections() and all(n in measured for n in home):
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Gazebo controllers did not become ready')
    for service in ('/gazebo/pause_physics', '/gazebo/unpause_physics', '/gazebo/set_model_configuration'):
        rospy.wait_for_service(service, timeout=10)
    pause = rospy.ServiceProxy('/gazebo/pause_physics', Empty)
    resume = rospy.ServiceProxy('/gazebo/unpause_physics', Empty)
    configure = rospy.ServiceProxy('/gazebo/set_model_configuration', SetModelConfiguration)
    msg = JointCommand()
    msg.mode = JointCommand.POSITION_MODE
    msg.names, msg.position = list(home), list(home.values())
    # This is scene initialization, not a teleport/reset available during A control.
    import rosnode
    wait_for_spawner(rosnode.get_node_names)
    pause()
    try:
        result = configure('sawyer', 'robot_description', list(home) +
                           ['generic_left_finger_joint', 'generic_right_finger_joint'],
                           list(home.values()) + [.04, .04])
        if not result.success:
            raise RuntimeError(result.status_message)
        for _ in range(10):
            command.publish(msg)
            for publisher in fingers:
                publisher.publish(Float64(data=.04))
            time.sleep(.02)
    finally:
        resume()
    end = time.monotonic() + 10
    stable_since = None
    while time.monotonic() < end and not rospy.is_shutdown():
        command.publish(msg)
        for publisher in fingers:
            publisher.publish(Float64(data=.04))
        if time.monotonic() - received[0] < .1 and all(
                abs(measured.get(name, float('inf')) - value) < .03 for name, value in home.items()):
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= 1:
                print('Initial pose verified; starting local Gazebo B.', flush=True)
                return
        else:
            stable_since = None
        time.sleep(.02)
    raise RuntimeError('Initial pose did not settle; B was not started')


if __name__ == '__main__':
    main()
