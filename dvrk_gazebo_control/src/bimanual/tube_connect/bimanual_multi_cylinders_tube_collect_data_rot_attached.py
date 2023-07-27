#!/usr/bin/env python3
from __future__ import print_function, division, absolute_import


import sys
import roslib.packages as rp
pkg_path = rp.get_pkg_dir('dvrk_gazebo_control')
sys.path.append(pkg_path + '/src')
import os
import math
import numpy as np
from isaacgym import gymapi
from isaacgym import gymtorch
from isaacgym import gymutil
from copy import copy, deepcopy
import rospy
# from dvrk_gazebo_control.srv import *
from geometry_msgs.msg import PoseStamped, Pose
from GraspDataCollectionClient import GraspDataCollectionClient
import open3d
from utils import open3d_ros_helper as orh
from utils import o3dpc_to_GraspObject_msg as o3dpc_GO
#import pptk
from utils.isaac_utils import isaac_format_pose_to_PoseStamped as to_PoseStamped
from utils.isaac_utils import fix_object_frame, get_pykdl_client
# from utils.record_data_h5 import RecordGraspData_sparse
import pickle
# from ShapeServo import *
# from sklearn.decomposition import PCA
import timeit
from copy import deepcopy
from PIL import Image
import transformations
import random

from core import Robot
from behaviors import MoveToPose, TaskVelocityControl2

sys.path.append('/home/baothach/shape_servo_DNN')
from farthest_point_sampling import *


ROBOT_Z_OFFSET = 0.25
# angle_kuka_2 = -0.4
# init_kuka_2 = 0.15
two_robot_offset = 1.0



def init():
    for i in range(num_envs):
        # Kuka 1
        davinci_dof_states = gym.get_actor_dof_states(envs[i], kuka_handles[i], gymapi.STATE_NONE)
        davinci_dof_states['pos'][4] = 0.2
        davinci_dof_states['pos'][8] = 1.5
        davinci_dof_states['pos'][9] = 0.8
        gym.set_actor_dof_states(envs[i], kuka_handles[i], davinci_dof_states, gymapi.STATE_POS)

        # # Kuka 2
        davinci_dof_states = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_NONE)
        davinci_dof_states['pos'][4] = 0.2
        davinci_dof_states['pos'][8] = 1.5
        davinci_dof_states['pos'][9] = 0.8
        gym.set_actor_dof_states(envs[i], kuka_handles_2[i], davinci_dof_states, gymapi.STATE_POS)

def get_point_cloud():
    gym.refresh_particle_state_tensor(sim)
    particle_state_tensor = deepcopy(gymtorch.wrap_tensor(gym.acquire_particle_state_tensor(sim)))
    point_cloud = particle_state_tensor.numpy()[:, :3]  
    
    # pcd = open3d.geometry.PointCloud()
    # pcd.points = open3d.utility.Vector3dVector(np.array(point_cloud))
    # open3d.visualization.draw_geometries([pcd])     
    # return list(point_cloud)
    return point_cloud.astype('float32')


def get_partial_point_cloud(i):

    # Render all of the image sensors only when we need their output here
    # rather than every frame.
    gym.render_all_camera_sensors(sim)

    points = []
    print("Converting Depth images to point clouds. Have patience...")
    # for c in range(len(cam_handles)):
    
    # print("Deprojecting from camera %d, %d" % i))
    # Retrieve depth and segmentation buffer
    depth_buffer = gym.get_camera_image(sim, envs_obj[i], cam_handles[i], gymapi.IMAGE_DEPTH)
    seg_buffer = gym.get_camera_image(sim, envs_obj[i], cam_handles[i], gymapi.IMAGE_SEGMENTATION)


    # Get the camera view matrix and invert it to transform points from camera to world
    # space
    
    vinv = np.linalg.inv(np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], cam_handles[0])))

    # Get the camera projection matrix and get the necessary scaling
    # coefficients for deprojection
    proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
    fu = 2/proj[0, 0]
    fv = 2/proj[1, 1]

    # Ignore any points which originate from ground plane or empty space
    depth_buffer[seg_buffer == 11] = -10001

    centerU = cam_width/2
    centerV = cam_height/2
    for k in range(cam_width):
        for t in range(cam_height):
            if depth_buffer[t, k] < -3:
                continue

            u = -(k-centerU)/(cam_width)  # image-space coordinate
            v = (t-centerV)/(cam_height)  # image-space coordinate
            d = depth_buffer[t, k]  # depth buffer value
            X2 = [d*fu*u, d*fv*v, d, 1]  # deprojection vector
            p2 = X2*vinv  # Inverse camera view to get world coordinates
            # print("p2:", p2)
            if p2[0, 2] > 0.01:
                points.append([p2[0, 0], p2[0, 1], p2[0, 2]])

    # pcd = open3d.geometry.PointCloud()
    # pcd.points = open3d.utility.Vector3dVector(np.array(points))
    # open3d.visualization.draw_geometries([pcd]) 

    # return points
    return np.array(points).astype('float32')

def get_point_clouds(vis=False):
    gym.refresh_particle_state_tensor(sim)
    particle_state_tensor = deepcopy(gymtorch.wrap_tensor(gym.acquire_particle_state_tensor(sim)))
    point_cloud = particle_state_tensor.numpy()[:, :3]  
    assert point_cloud.shape[0] % 2 == 0
    num_point = int(point_cloud.shape[0] / 2)
    
    if vis:
        pc_1 = point_cloud[:num_point]
        pc_2 = point_cloud[num_point:]
        pcd = open3d.geometry.PointCloud()
        pcd.points = open3d.utility.Vector3dVector(pc_1)
        colors = np.zeros(pc_1.shape)
        colors[3*num_point//4:, 0] = 1
        pcd.colors = open3d.utility.Vector3dVector(colors)
        pcd_2 = open3d.geometry.PointCloud()
        pcd_2.points = open3d.utility.Vector3dVector(pc_2)
        open3d.visualization.draw_geometries([pcd, pcd_2]) 

    return point_cloud[:num_point], point_cloud[num_point:]

def down_sampling(pc, num_point=1024):
    farthest_indices,_ = farthest_point_sampling(pc, num_point)
    pc = pc[farthest_indices.squeeze()]  
    return pc

def increment(x, y, z, deltas = [0,0,0]):
    return x + deltas[0], y + deltas[1], z + deltas[2]

if __name__ == "__main__":

    rospy.init_node('isaac_grasp_client')
    random_movement = random.choice([True, False]) 
    data_type = "random" if random_movement else "maintain alignment"
    

    # initialize gym
    gym = gymapi.acquire_gym()

    # parse arguments
    args = gymutil.parse_arguments(
        description="Kuka Bin Test",
        custom_parameters=[
            {"name": "--num_envs", "type": int, "default": 1, "help": "Number of environments to create"},
            {"name": "--num_objects", "type": int, "default": 10, "help": "Number of objects in the bin"},
            {"name": "--obj_name", "type": str, "default": 'cylinder_0', "help": "select variations of a primitive shape"},
            {"name": "--headless", "type": str, "default": "False", "help": "headless mode"}])

    num_envs = args.num_envs
    args.headless = args.headless == "True"

    


    # configure sim
    sim_type = gymapi.SIM_FLEX
    sim_params = gymapi.SimParams()
    sim_params.up_axis = gymapi.UP_AXIS_Z
    sim_params.gravity = gymapi.Vec3(0.0, 0.0, -9.8)
    if sim_type is gymapi.SIM_FLEX:
        sim_params.substeps = 4
        # print("=================sim_params.dt:", sim_params.dt)
        sim_params.dt = 1./60.
        sim_params.flex.solver_type = 5
        sim_params.flex.num_outer_iterations = 4
        sim_params.flex.num_inner_iterations = 50
        sim_params.flex.relaxation = 0.7
        sim_params.flex.warm_start = 0.1
        sim_params.flex.shape_collision_distance = 5e-4
        sim_params.flex.contact_regularization = 1.0e-6
        sim_params.flex.shape_collision_margin = 1.0e-4
        sim_params.flex.deterministic_mode = True

    sim = gym.create_sim(args.compute_device_id, args.graphics_device_id, sim_type, sim_params)

    # Get primitive shape dictionary to know the dimension of the object   
    object_meshes_path = "/home/baothach/sim_data/Custom/Custom_mesh/multi_cylinders_official"    
    with open(os.path.join(object_meshes_path, "primitive_dict_cylinder.pickle"), 'rb') as handle:
        data = pickle.load(handle)    
    r = 0.02 #data[args.obj_name]["radius"]
    h = data[args.obj_name]["height"]


    # add ground plane
    plane_params = gymapi.PlaneParams()
    plane_params.normal = gymapi.Vec3(0, 0, 1) # z-up ground
    gym.add_ground(sim, plane_params)

    # create viewer
    if not args.headless:
        viewer = gym.create_viewer(sim, gymapi.CameraProperties())
        if viewer is None:
            print("*** Failed to create viewer")
            quit()

    # load robot assets
    pose = gymapi.Transform()
    pose.p = gymapi.Vec3(0.0, -two_robot_offset, ROBOT_Z_OFFSET) 
 
    pose_2 = gymapi.Transform()
    pose_2.p = gymapi.Vec3(0.0, 0.0, ROBOT_Z_OFFSET)
    pose_2.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

    asset_options = gymapi.AssetOptions()
    asset_options.armature = 0.001
    asset_options.fix_base_link = True
    asset_options.thickness = 0.001 #0.0005


    asset_root = "./src/dvrk_env"
    kuka_asset_file = "dvrk_description/psm/psm_for_issacgym.urdf"


    asset_options.fix_base_link = True
    asset_options.flip_visual_attachments = False
    asset_options.collapse_fixed_joints = True
    asset_options.disable_gravity = True
    asset_options.default_dof_drive_mode = gymapi.DOF_MODE_POS

    if sim_type is gymapi.SIM_FLEX:
        asset_options.max_angular_velocity = 40000.

    print("Loading asset '%s' from '%s'" % (kuka_asset_file, asset_root))
    kuka_asset = gym.load_asset(sim, asset_root, kuka_asset_file, asset_options)


    # asset_root = '/home/baothach/sim_data/Custom/Custom_urdf/multi_cylinders/'
    # soft_asset_file = 'cylinder_2_attached_tube.urdf'
    asset_root = '/home/baothach/sim_data/Custom/Custom_urdf/multi_cylinders/'
    soft_asset_file = 'cylinder_2_attached.urdf'

    soft_pose_2 = gymapi.Transform()    
    soft_pose_2.p = gymapi.Vec3(0, -0.46, r/2.0)   
    soft_pose_2.r = gymapi.Quat(0.7071068, 0, 0, 0.7071068)


    soft_pose_1 = gymapi.Transform()
    pos_1_x = np.random.uniform(low = 0.05, high = 0.13)
    pos_1_y = np.random.uniform(low = -0.46, high = -0.35)
    soft_pose_1.p = gymapi.Vec3(pos_1_x, pos_1_y, r/2.0)
    

    rot_angle = 0.0 #np.random.uniform(low = -np.pi/6, high = np.pi/6)
    quat = transformations.quaternion_from_euler(*[0, 0, -np.pi/2])
    soft_pose_1.r = gymapi.Quat(*quat)

    pose.p = gymapi.Vec3(soft_pose_1.p.x, soft_pose_1.p.y+soft_pose_2.p.y, ROBOT_Z_OFFSET)

    soft_thickness = 0.01#0.0005    # important to add some thickness to the soft body to avoid interpenetrations


    asset_options = gymapi.AssetOptions()
    asset_options.fix_base_link = True
    asset_options.thickness = soft_thickness
    asset_options.disable_gravity = True

    soft_asset = gym.load_asset(sim, asset_root, soft_asset_file, asset_options)

        
    
 
    
    # set up the env grid
    # spacing = 0.75
    spacing = 0.0
    env_lower = gymapi.Vec3(-spacing, 0.0, -spacing)
    env_upper = gymapi.Vec3(spacing, spacing, spacing)
  

    # cache some common handles for later use
    envs = []
    envs_obj = []
    kuka_handles = []
    kuka_handles_2 = []
    object_handles = []
    

    print("Creating %d environments" % num_envs)
    num_per_row = int(math.sqrt(num_envs))
    base_poses = []

    for i in range(num_envs):
        # create env
        env = gym.create_env(sim, env_lower, env_upper, num_per_row)
        envs.append(env)

        # add kuka
        kuka_handle = gym.create_actor(env, kuka_asset, pose, "kuka", i, 1, segmentationId=11)

        # add kuka2
        kuka_2_handle = gym.create_actor(env, kuka_asset, pose_2, "kuka2", i+1, 1, segmentationId=11)        
        

        # add soft obj        
        env_obj = env
        # env_obj = gym.create_env(sim, env_lower, env_upper, num_per_row)
        envs_obj.append(env_obj)        
        
        soft_actor_1 = gym.create_actor(env_obj, soft_asset, soft_pose_1, "soft_1", i, 0)
        soft_actor_2 = gym.create_actor(env_obj, soft_asset, soft_pose_2, "soft_2", i+1, 0)
        object_handles = [soft_actor_1, soft_actor_2]

        kuka_handles.append(kuka_handle)
        kuka_handles_2.append(kuka_2_handle)



    dof_props_2 = gym.get_asset_dof_properties(kuka_asset)
    dof_props_2["driveMode"].fill(gymapi.DOF_MODE_POS)
    dof_props_2["stiffness"].fill(200.0)
    dof_props_2["damping"].fill(40.0)
    dof_props_2["stiffness"][8:].fill(1)
    dof_props_2["damping"][8:].fill(2)  
    vel_limits = dof_props_2['velocity']  
    print("======vel_limits:", vel_limits)  
    

    # Camera setup
    if not args.headless:
        cam_pos = gymapi.Vec3(1, 0.5, 1)
        # cam_pos = gymapi.Vec3(1, -0.5, 1)
        # cam_pos = gymapi.Vec3(0.3, -0.7, 0.3)
        # cam_pos = gymapi.Vec3(0.3, -0.1, 0.5)  # final setup for thin layer tissue
        # cam_pos = gymapi.Vec3(0.5, -0.36, 0.3)
        # cam_target = gymapi.Vec3(0.0, 0.0, 0.1)
        cam_target = gymapi.Vec3(0.0, -0.36, 0.1)
        middle_env = envs[num_envs // 2 + num_per_row // 2]
        gym.viewer_camera_look_at(viewer, middle_env, cam_pos, cam_target)

    # Camera for point cloud setup
    cam_positions = []
    cam_targets = []
    cam_handles = []
    cam_width = 256
    cam_height = 256
    cam_props = gymapi.CameraProperties()
    cam_props.width = cam_width
    cam_props.height = cam_height
    # cam_positions.append(gymapi.Vec3(0.12, -0.55, 0.15))
    # cam_positions.append(gymapi.Vec3(0.1, -0.5-(two_robot_offset/2 - 0.42), 0.2))
    # cam_targets.append(gymapi.Vec3(0.0, -0.45-(two_robot_offset/2 - 0.42), 0.00))

    
    cam_positions.append(gymapi.Vec3(0.20, -two_robot_offset/2, 0.2))
    cam_targets.append(gymapi.Vec3(0.0, -two_robot_offset/2, 0.01)) 
    # cam_positions.append(gymapi.Vec3(0.17, -0.62-(two_robot_offset/2 - 0.42), 0.2))
    # cam_targets.append(gymapi.Vec3(0.0, 0.40-0.86-(two_robot_offset/2 - 0.42), 0.01))  

    
    for i, env_obj in enumerate(envs_obj):
            cam_handles.append(gym.create_camera_sensor(env_obj, cam_props))
            gym.set_camera_location(cam_handles[i], env_obj, cam_positions[0], cam_targets[0])



    # set dof properties
    for env in envs:
        gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)
        gym.set_actor_dof_properties(env, kuka_handles[i], dof_props_2)

        

    '''
    Main stuff is here
    '''
    # rospy.logerr("======Loading object ... " + str(args.obj_name))  
    rospy.logerr(f"random movement: {random_movement}") 

    # Some important paramters
    init()  # Initilize 2 robots' joints
    all_done = False
    state = "home"
    
    
    data_recording_path = "/home/baothach/shape_servo_data/tube_connect/cylinder_rot_attached/data"
    terminate_count = 0
    sample_count = 0
    frame_count = 0
    group_count = 0
    data_point_count = len(os.listdir(data_recording_path))
    max_group_count = 10#10
    max_sample_count = 1 
    max_data_point_count = 20000
    # if args.obj_name == 'cylinder_64':
    #     max_data_point_per_variation = 9600
    # else:
    max_data_point_per_variation = data_point_count + 100
    rospy.logwarn("max_data_point_per_variation:" + str(max_data_point_per_variation))

    maintain_alignment = True if not random_movement else False
    pc_on_trajectory = []
    full_pc_on_trajectory = []
    curr_trans_on_trajectory_1 = []  
    curr_trans_on_trajectory_2 = [] 
    first_time = True
    save_intial_pc = True
    switch = True
    data = []
    preset_deltas = None
    



    dc_client = GraspDataCollectionClient()
    


    
    start_time = timeit.default_timer()    

    close_viewer = False

    robot_2 = Robot(gym, sim, envs[0], kuka_handles_2[0])
    robot_1 = Robot(gym, sim, envs[0], kuka_handles[0])

    while (not close_viewer) and (not all_done): 



        if not args.headless:
            close_viewer = gym.query_viewer_has_closed(viewer)  

        # step the physics
        gym.simulate(sim)
        gym.fetch_results(sim, True)
 

        if state == "home" :   
            frame_count += 1
            gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_main_insertion_joint"), 0.203)
            gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_main_insertion_joint"), 0.203)            
            if frame_count == 5:
                rospy.loginfo("**Current state: " + state + ", current sample count: " + str(sample_count))
                

                if first_time:                    
                    gym.refresh_particle_state_tensor(sim)
                    saved_object_state = deepcopy(gymtorch.wrap_tensor(gym.acquire_particle_state_tensor(sim))) 
                    saved_frame_state_1 = deepcopy(gym.get_actor_rigid_body_states(envs_obj[i], object_handles[0], gymapi.STATE_ALL))
                    saved_frame_state_2 = deepcopy(gym.get_actor_rigid_body_states(envs_obj[i], object_handles[1], gymapi.STATE_ALL))

                    init_robot_state_2 = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_ALL))
                    init_robot_state_1 = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles[i], gymapi.STATE_ALL))
                    first_time = False

                    pc_1, pc_2 = get_point_clouds(vis=False)

                    pcd_1 = open3d.geometry.PointCloud()
                    pcd_1.points = open3d.utility.Vector3dVector(pc_1)
                    open3d.io.write_point_cloud("/home/baothach/shape_servo_data/multi_grasps/1.pcd", pcd_1) # save_grasp_visual_data , point cloud of the object
                    pc_ros_msg = dc_client.seg_obj_from_file_client(pcd_file_path = "/home/baothach/shape_servo_data/multi_grasps/1.pcd", align_obj_frame = False).obj
                    pc_ros_msg_1 = fix_object_frame(pc_ros_msg)
                
                    pcd_2 = open3d.geometry.PointCloud()
                    pcd_2.points = open3d.utility.Vector3dVector(pc_2)
                    open3d.io.write_point_cloud("/home/baothach/shape_servo_data/multi_grasps/1.pcd", pcd_2) # save_grasp_visual_data , point cloud of the object
                    pc_ros_msg = dc_client.seg_obj_from_file_client(pcd_file_path = "/home/baothach/shape_servo_data/multi_grasps/1.pcd", align_obj_frame = False).obj
                    pc_ros_msg_2 = fix_object_frame(pc_ros_msg)

                state = "generate preshape"                
                frame_count = 0              


        if state == "generate preshape":                   
            rospy.loginfo("**Current state: " + state)
            preshape_response = dc_client.gen_grasp_preshape_client(pc_ros_msg_2)               
            cartesian_goal = deepcopy(preshape_response.palm_goal_pose_world[0].pose)        
            target_pose = [-cartesian_goal.position.x, -cartesian_goal.position.y, cartesian_goal.position.z-ROBOT_Z_OFFSET,
                            0, 0.707107, 0.707107, 0]
            mtp_behavior_2 = MoveToPose(target_pose, robot_2, sim_params.dt, 1)
            
            preshape_response = dc_client.gen_grasp_preshape_client(pc_ros_msg_1, non_random=True)               
            cartesian_goal = deepcopy(preshape_response.palm_goal_pose_world[0].pose)        
            target_pose = [cartesian_goal.position.x-pose.p.x, cartesian_goal.position.y - pose.p.y, cartesian_goal.position.z-ROBOT_Z_OFFSET,
                            0, 0.707107, 0.707107, 0]
            mtp_behavior_1 = MoveToPose(target_pose, robot_1, sim_params.dt, 1)            
            
            if mtp_behavior_1.is_complete_failure() or mtp_behavior_2.is_complete_failure():
                rospy.logerr('Can not find moveit plan to grasp. Ignore this grasp.\n')  
                state = "reset"                
            else:
                rospy.loginfo('Sucesfully found a PRESHAPE moveit plan to grasp.\n')
                state = "move to preshape"
                # rospy.loginfo('Moving to this preshape goal: ' + str(cartesian_goal))



        if state == "move to preshape":         
            action_1 = mtp_behavior_1.get_action()
            action_2 = mtp_behavior_2.get_action()

            if action_1 is not None:
                gym.set_actor_dof_position_targets(robot_1.env_handle, robot_1.robot_handle, action_1.get_joint_position())      
                prev_action_1 = action_1
            else:
                gym.set_actor_dof_position_targets(robot_1.env_handle, robot_1.robot_handle, prev_action_1.get_joint_position())

            if action_2 is not None:
                gym.set_actor_dof_position_targets(robot_2.env_handle, robot_2.robot_handle, action_2.get_joint_position())      
                prev_action_2 = action_2
            else:
                gym.set_actor_dof_position_targets(robot_2.env_handle, robot_2.robot_handle, prev_action_2.get_joint_position())


            if mtp_behavior_1.is_complete() and mtp_behavior_2.is_complete():
                state = "grasp object"   
                rospy.loginfo("Succesfully executed PRESHAPE moveit arm plan. Let's fucking grasp it!!") 

        
        if state == "grasp object":             
            rospy.loginfo("**Current state: " + state)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper1_joint"), -2.5)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper2_joint"), -3.0)         

            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka", "psm_tool_gripper1_joint"), -2.5)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka", "psm_tool_gripper2_joint"), -3.0)  

            g_1_pos = 0.4
            g_2_pos = -0.4
            dof_states_1 = gym.get_actor_dof_states(envs[i], kuka_handles[i], gymapi.STATE_POS)
            dof_states_2 = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)
            if dof_states_1['pos'][8] < 0.4 and dof_states_2['pos'][8] < 0.4:
                                       
                state = "get shape servo plan"

                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka", "psm_tool_gripper1_joint"), g_1_pos)
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka", "psm_tool_gripper2_joint"), g_2_pos)                     
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper1_joint"), g_1_pos)
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper2_joint"), g_2_pos)         
        
                current_pose_1 = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles[i], gymapi.STATE_POS)[-3])["pose"]["p"]
                current_pose_2 = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])["pose"]["p"]
                _,init_pose_1 = get_pykdl_client(robot_1.get_arm_joint_positions())
                init_eulers_1 = transformations.euler_from_matrix(init_pose_1)               
                _,init_pose_2 = get_pykdl_client(robot_2.get_arm_joint_positions())
                init_eulers_2 = transformations.euler_from_matrix(init_pose_2)   
                # print(init_eulers_1, init_eulers_2)    
                # print("init_pose_2:", init_pose_2[:3,3])   
                # print(1.35*two_robot_offset/3.5)       

                # switch to velocity mode
                dof_props_2['driveMode'][:8].fill(gymapi.DOF_MODE_VEL)
                dof_props_2["stiffness"][:8].fill(0.0)
                dof_props_2["damping"][:8].fill(200.0)
                gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)
                gym.set_actor_dof_properties(env, kuka_handles[i], dof_props_2)
               
                

        if state == "get shape servo plan":
            rospy.loginfo("**Current state: " + state) 
            max_y = max_z = h * 0.8 / 2.0
            if soft_pose_1.p.x >= 0.06:
                max_x = soft_pose_1.p.x 
            else:
                max_x = soft_pose_1.p.x / 1.5
            max_y *= 2/3
            max_z *= 3/4
            # print("max_x, max_y, max_z:", max_x, max_y, max_z)

            delta_alpha = np.random.uniform(low = -np.pi/3, high = np.pi/3)
            delta_beta = np.random.uniform(low = -np.pi/3, high = np.pi/3) 
            delta_gamma = np.random.uniform(low = -np.pi/2, high = np.pi/2)

            delta_x = np.random.uniform(low = -max_x, high = 0)
            delta_y = np.random.uniform(low = 0.0, high = max_y)
            delta_z = np.random.uniform(low = 0.0, high = max_z)      


            x = delta_x + init_pose_1[0,3]
            y = delta_y + init_pose_1[1,3]
            z = delta_z + init_pose_1[2,3]
            alpha = delta_alpha + init_eulers_1[0]
            beta = delta_beta + init_eulers_1[1]
            gamma = delta_gamma + init_eulers_1[2]

            tvc_behavior_1 = TaskVelocityControl2([x,y,z,alpha,beta,gamma], robot_1, sim_params.dt, 3, vel_limits=vel_limits)
            



            delta_alpha = np.random.uniform(low = -np.pi/3, high = np.pi/3)
            delta_beta = np.random.uniform(low = -np.pi/3, high = np.pi/3) 
            delta_gamma = np.random.uniform(low = -np.pi/2, high = np.pi/2)

            delta_x = np.random.uniform(low = -max_x, high = 0)
            delta_y = np.random.uniform(low = 0.0, high = max_y)
            delta_z = np.random.uniform(low = 0.0, high = max_z) 

            x = delta_x + init_pose_2[0,3]
            y = delta_y + init_pose_2[1,3]
            z = delta_z + init_pose_2[2,3]
            alpha = delta_alpha + init_eulers_2[0]
            beta = delta_beta + init_eulers_2[1]
            gamma = delta_gamma + init_eulers_2[2]

            tvc_behavior_2 = TaskVelocityControl2([x,y,z,alpha,beta,gamma], robot_2, sim_params.dt, 3, vel_limits=vel_limits)        

            closed_loop_start_time = deepcopy(gym.get_sim_time(sim))
            state = "move to goal"


        if state == "move to goal":
            insertion_1 = gym.get_actor_dof_states(envs[i], kuka_handles[i], gymapi.STATE_POS)['pos'][4]
            insertion_2 = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)['pos'][4]
            # print("insertion_1, insertion_2:", insertion_1, insertion_2)
            if insertion_1 < 0.042 or insertion_2 < 0.042:
                state = "reset"
                rospy.logerr("insertion too short")

            # if random_movement:
            #     rigid_contacts = gym.get_env_rigid_contacts(envs[0])
            #     if len(list(rigid_contacts)) != 0:
            #         for k in range(len(list(rigid_contacts))):
            #             if rigid_contacts[k]['body0'] > -1 and rigid_contacts[k]['body1'] > -1 : # ignore collision with the ground which has a value of -1
            #                 state = "reset"
            #                 rospy.logerr("Two robots collided !!")
            #                 all_done = True
            #                 break 

            contacts = [contact[4] for contact in gym.get_soft_contacts(sim)]
            if (not(20 in contacts or 21 in contacts) or not(9 in contacts or 10 in contacts)):  # lose contact w either robot 2 or robot 1    
                rospy.logerr("Lost contact with robot")
                
                # group_count += 1
                state = "reset"
                all_done = True

            else:
                if frame_count % 15 == 0:
                    full_pc_on_trajectory.append(get_point_cloud())
                    pc_on_trajectory.append(get_partial_point_cloud(i))
                    curr_trans_on_trajectory_1.append(get_pykdl_client(robot_1.get_arm_joint_positions())[1])
                    curr_trans_on_trajectory_2.append(get_pykdl_client(robot_2.get_arm_joint_positions())[1])
              

                    terminate_count += 1
                    if terminate_count >= 15:
                        print("max terminate count")
                        state = "reset"
                        terminate_count = 0
                frame_count += 1   
     
                                
                action_1 = tvc_behavior_1.get_action()  
                action_2 = tvc_behavior_2.get_action() 
                
                if not(action_1 is None or action_2 is None) and gym.get_sim_time(sim) - closed_loop_start_time <= 4:   
                    gym.set_actor_dof_velocity_targets(robot_1.env_handle, robot_1.robot_handle, action_1.get_joint_position())
                    gym.set_actor_dof_velocity_targets(robot_2.env_handle, robot_2.robot_handle, action_2.get_joint_position())
                    
                else:   
                    rospy.loginfo("Succesfully executed moveit arm plan. Let's record point cloud!!")  

                    # frame_1 = gym.get_actor_rigid_body_states(envs[i], object_handles[0], gymapi.STATE_POS)['pose']['p']
                    # frame_2 = gym.get_actor_rigid_body_states(envs[i], object_handles[1], gymapi.STATE_POS)['pose']['p']  
                    # error = np.array(list(frame_2[0])) - np.array(list(frame_1[0]))                   
                    
                    # rospy.logerr(f"final error between two tubes: {error}")
                    # # get_point_clouds(vis=True)  

                  

                    pc_goal = get_partial_point_cloud(i)
                    full_pc_goal = get_point_cloud()
                    _, final_trans_1 = get_pykdl_client(robot_1.get_arm_joint_positions())
                    _, final_trans_2 = get_pykdl_client(robot_2.get_arm_joint_positions())
                    # print("***Final x, y, z: ", final_pose["pose"]["p"]["x"], final_pose["pose"]["p"]["y"], final_pose["pose"]["p"]["z"] ) 
                    
                    for j, (curr_trans_1, curr_trans_2) in enumerate(zip(curr_trans_on_trajectory_1, curr_trans_on_trajectory_2)):
                        p_1, R_1, twist_1 = tvc_behavior_1.get_transform(curr_trans_1, final_trans_1, get_twist=True)
                        mani_point_1 = curr_trans_1

                        p_2, R_2, twist_2 = tvc_behavior_2.get_transform(curr_trans_2, final_trans_2, get_twist=True)
                        mani_point_2 = curr_trans_2

                        partial_pcs = (pc_on_trajectory[j], pc_goal)
                        full_pcs = (full_pc_on_trajectory[j], full_pc_goal)


                        data = {"full pcs": full_pcs, "partial pcs": partial_pcs, \
                                "pos_1": p_1, "rot_1": R_1, "twist_1": twist_1, \
                                "pos_2": p_2, "rot_2": R_2, "twist_2": twist_2,     
                                "mani_point_1": mani_point_1, "mani_point_2": mani_point_2, \
                                "two_robot_offset": pose.p}
                        with open(os.path.join(data_recording_path, "sample " + str(data_point_count) + ".pickle"), 'wb') as handle:
                            pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)                    
                        

                        print("data_point_count:", data_point_count)
                        data_point_count += 1            
                                 

                    frame_count = 0
                    terminate_count = 0
                    sample_count += 1
                    print("group ", group_count, ", sample ", sample_count)
                    pc_on_trajectory = []
                    full_pc_on_trajectory = []
                    curr_trans_on_trajectory_1 = []  
                    curr_trans_on_trajectory_2 = []
                    full_pc_on_trajectory = []
                    pc_on_trajectory = []                    
                    state = "get shape servo plan"




        if state == "reset":   
            rospy.loginfo("**Current state: " + state)
            frame_count = 0
            sample_count = 0
            terminate_count = 0

            dof_props_2['driveMode'][:8].fill(gymapi.DOF_MODE_POS)
            dof_props_2["stiffness"][:8].fill(200.0)
            dof_props_2["damping"][:8].fill(40.0)

            gym.set_actor_rigid_body_states(envs_obj[i], object_handles[0], saved_frame_state_1, gymapi.STATE_ALL)
            gym.set_actor_rigid_body_states(envs_obj[i], object_handles[1], saved_frame_state_2, gymapi.STATE_ALL)
            gym.set_actor_rigid_body_states(envs[i], kuka_handles[i], init_robot_state_1, gymapi.STATE_ALL) 
            gym.set_actor_rigid_body_states(envs[i], kuka_handles_2[i], init_robot_state_2, gymapi.STATE_ALL) 
            gym.set_particle_state_tensor(sim, gymtorch.unwrap_tensor(saved_object_state))
            gym.set_actor_dof_velocity_targets(robot_1.env_handle, robot_1.robot_handle, [0]*8)
            gym.set_actor_dof_velocity_targets(robot_2.env_handle, robot_2.robot_handle, [0]*8)

            
            print("Sucessfully reset robot and object")
            pc_on_trajectory = []
            full_pc_on_trajectory = []
            curr_trans_on_trajectory_1 = []  
            curr_trans_on_trajectory_2 = []
                
            gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)
            gym.set_actor_dof_properties(env, kuka_handles[i], dof_props_2)

            state = "home"
            # all_done = True
 
        
        if sample_count >= max_sample_count:  
            sample_count = 0            
            group_count += 1
            print("group count: ", group_count)
            state = "reset" 


        if group_count == max_group_count:
            rospy.logerr("Reach max group_count")                 
            all_done = True 
            
        # if group_count == max_group_count or data_point_count >= max_data_point_count: 
        if  data_point_count >= max_data_point_count:   
            rospy.logerr("Reach max data count")                 
            all_done = True 

        # step rendering
        gym.step_graphics(sim)
        if not args.headless:
            gym.draw_viewer(viewer, sim, False)


  
   



    print("All done !")
    print("Elapsed time", timeit.default_timer() - start_time)
    if not args.headless:
        gym.destroy_viewer(viewer)
    gym.destroy_sim(sim)
    print("total data pt count: ", data_point_count)
