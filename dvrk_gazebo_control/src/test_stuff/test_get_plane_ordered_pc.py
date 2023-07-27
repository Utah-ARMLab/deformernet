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
from utils.isaac_utils import fix_object_frame
# from utils.record_data_h5 import RecordGraspData_sparse
import pickle
# from ShapeServo import *
# from sklearn.decomposition import PCA
import timeit
from copy import deepcopy
from PIL import Image
from shape_servo.fit_plane import *
from core import Robot
from behaviors import MoveToPose, TaskVelocityControl



ROBOT_Z_OFFSET = 0.30
# angle_kuka_2 = -0.4
# init_kuka_2 = 0.15
two_robot_offset = 0.86



def init():
    for i in range(num_envs):
        # # Kuka 2
        davinci_dof_states = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_NONE)
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
    seg_buffer = gym.get_camera_image(sim, envs[i], cam_handles[i], gymapi.IMAGE_SEGMENTATION)


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
            if p2[0, 2] > 0.03:
                points.append([p2[0, 0], p2[0, 1], p2[0, 2]])

    # pcd = open3d.geometry.PointCloud()
    # pcd.points = open3d.utility.Vector3dVector(np.array(points))
    # open3d.visualization.draw_geometries([pcd]) 

    # return points
    return np.array(points).astype('float32')

def convert_unordered_to_ordered_pc(goal_pc, i, get_depth_img = False):
    # proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
    # fu = 2/proj[0, 0]
    # fv = 2/proj[1, 1]
    

    u_s =[]
    v_s = []

    for point in goal_pc:
        point = list(point) + [1]

        point = np.expand_dims(np.array(point), axis=0)

        point_cam_frame = point * np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], cam_handles[0]))
        # print("point_cam_frame:", point_cam_frame)
        # image_coordinates = (gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[0]) * point_cam_frame)
        # print("image_coordinates:",image_coordinates)
        # u_s.append(image_coordinates[1, 0]/image_coordinates[2, 0]*2)
        # v_s.append(image_coordinates[0, 0]/image_coordinates[2, 0]*2)
        # print("fu fv:", fu, fv)
        u_s.append(1/2 * point_cam_frame[0, 0]/point_cam_frame[0, 2])
        v_s.append(1/2 * point_cam_frame[0, 1]/point_cam_frame[0, 2])      
          
    centerU = cam_width/2
    centerV = cam_height/2    
    # print(centerU - np.array(u_s)*cam_width)
    # y_s = (np.array(u_s)*cam_width).astype(int)
    # x_s = (np.array(v_s)*cam_height).astype(int)
    y_s = (centerU - np.array(u_s)*cam_width).astype(int)
    x_s = (centerV + np.array(v_s)*cam_height).astype(int)    
    
    points = np.zeros((cam_width, cam_height, 3))
    for t in range(len(x_s)):
        points[x_s[t], y_s[t]] = goal_pc[t]    
    # points[x_s, y_s] = goal_pc
    if get_depth_img == False:        

        return points
    else:
        img = np.zeros((cam_width, cam_height))
        img[x_s, y_s] = 255
        
        # print(img)
        return points, img

    return x_s, y_s


# def convert_unordered_to_ordered_pc(goal_pc, i, get_depth_img = False):
#     # proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
#     # fu = 2/proj[0, 0]
#     # fv = 2/proj[1, 1]
    

#     u_s =[]
#     v_s = []

#     for point in goal_pc:
#         point = list(point) + [1]

#         point = np.expand_dims(np.array(point), axis=0)

#         point_cam_frame = point * np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], second_cam_handles[0]))
#         # print("point_cam_frame:", point_cam_frame)
#         # image_coordinates = (gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[0]) * point_cam_frame)
#         # print("image_coordinates:",image_coordinates)
#         # u_s.append(image_coordinates[1, 0]/image_coordinates[2, 0]*2)
#         # v_s.append(image_coordinates[0, 0]/image_coordinates[2, 0]*2)
#         # print("fu fv:", fu, fv)
#         u_s.append(1/2 * point_cam_frame[0, 0]/point_cam_frame[0, 2])
#         v_s.append(1/2 * point_cam_frame[0, 1]/point_cam_frame[0, 2])      
          
#     centerU = second_cam_width/2
#     centerV = second_cam_height/2    
#     # print(centerU - np.array(u_s)*cam_width)
#     # y_s = (np.array(u_s)*cam_width).astype(int)
#     # x_s = (np.array(v_s)*cam_height).astype(int)
#     y_s = (centerU - np.array(u_s)*second_cam_width).astype(int)
#     x_s = (centerV + np.array(v_s)*second_cam_height).astype(int)    
    
#     points = np.zeros((second_cam_width, second_cam_height, 3))
#     for t in range(len(x_s)):
#         points[x_s[t], y_s[t]] = goal_pc[t]    
#     # points[x_s, y_s] = goal_pc
#     if get_depth_img == False:        

#         return points
#     else:
#         img = np.zeros((second_cam_width, second_cam_height))
#         img[x_s, y_s] = 255
        
#         # print(img)
#         return points, img

#     return x_s, y_s
if __name__ == "__main__":

    # initialize gym
    gym = gymapi.acquire_gym()

    # parse arguments
    args = gymutil.parse_arguments(
        description="Kuka Bin Test",
        custom_parameters=[
            {"name": "--num_envs", "type": int, "default": 1, "help": "Number of environments to create"},
            {"name": "--num_objects", "type": int, "default": 10, "help": "Number of objects in the bin"},
            {"name": "--object_type", "type": int, "default": 0, "help": "Type of bjects to place in the bin: 0 - box, 1 - meat can, 2 - banana, 3 - mug, 4 - brick, 5 - random"},
            {"name": "--headless", "type": bool, "default": False, "help": "headless mode"}])

    num_envs = args.num_envs
    


    # configure sim
    sim_type = gymapi.SIM_FLEX
    sim_params = gymapi.SimParams()
    sim_params.up_axis = gymapi.UP_AXIS_Z
    sim_params.gravity = gymapi.Vec3(0.0, 0.0, -9.8)
    if sim_type is gymapi.SIM_FLEX:
        sim_params.substeps = 2
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
    pose_2 = gymapi.Transform()
    pose_2.p = gymapi.Vec3(0.0, 0.0, ROBOT_Z_OFFSET)
    # pose_2.p = gymapi.Vec3(0.0, 0.85, ROBOT_Z_OFFSET)
    pose_2.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

    asset_options = gymapi.AssetOptions()
    asset_options.armature = 0.001
    asset_options.fix_base_link = True
    asset_options.thickness = 0.0001


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


    rigid_asset_root = "/home/baothach/sim_data/Custom/Custom_urdf"
    rigid_asset_file = "kidney_rigid.urdf"
    rigid_pose = gymapi.Transform()
    rigid_pose.p = gymapi.Vec3(0.00, 0.38-two_robot_offset, 0.03)
    # rigid_pose.r = gymapi.Quat(0.0, 0.0, -0.707107, 0.707107)
    # rigid_pose.r = gymapi.Quat(0.7071068, 0, 0, 0.7071068)
    rigid_pose.r = gymapi.Quat( 0.7071068, 0.7071068, 0, 0 )
    # rigid_pose.r = gymapi.Quat(0.5, 0.5, 0.5, 0.5)  #kidney 2
    asset_options.thickness = 0.003 # 0.002
    rigid_asset = gym.load_asset(sim, rigid_asset_root, rigid_asset_file, asset_options)



    
    # Load soft objects' assets
    # asset_root = "/home/baothach/sim_data/BigBird/BigBird_urdf_new" # Current directory
    # asset_root = "/home/baothach/sim_data/YCB/YCB_urdf"
    asset_root = "/home/baothach/sim_data/Custom/Custom_urdf"
    # asset_root = "/home/baothach/sim_data/BigBird/BigBird_urdf_attached"
    # soft_asset_file = "soft_box/soft_box.urdf"
    
    # soft_asset_file = "3m_high_tack_spray_adhesive.urdf"
    # soft_asset_file = "cheez_it_white_cheddar.urdf"
    # soft_asset_file = "cholula_chipotle_hot_sauce.urdf"
    # asset_root = '/home/baothach/sim_data/Bao_objects/urdf'
    # soft_asset_file = "long_bar.urdf"
    # soft_asset_file = "011_banana_attached.urdf"
    # soft_asset_file = "kidney_attached.urdf"
    # soft_asset_file = "cheez_it_attached.urdf"
    # soft_asset_file = 'thin_tissue_layer.urdf'
    soft_asset_file = 'thin_tissue_layer_attached.urdf'


    soft_pose = gymapi.Transform()
    # soft_pose.p = gymapi.Vec3(0.0, 0.50-two_robot_offset, 0.1)
    # soft_pose.p = gymapi.Vec3(0.05, 0.50-two_robot_offset, 0.1)
    soft_pose.p = gymapi.Vec3(0.035, 0.40-two_robot_offset, 0.081)  # 0.08
    soft_pose.r = gymapi.Quat(0.0, 0.0, 0.707107, 0.707107)   #for banana
    # soft_pose.r = gymapi.Quat(0.7071068, 0, 0, 0.7071068)
    soft_thickness = 0.0005    # important to add some thickness to the soft body to avoid interpenetrations





    asset_options = gymapi.AssetOptions()
    asset_options.fix_base_link = True
    asset_options.thickness = soft_thickness
    asset_options.disable_gravity = True
    # asset_options.default_dof_drive_mode = gymapi.DOF_MODE_POS

    # print("Loading asset '%s' from '%s'" % (soft_asset_file, asset_root))
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

        # add kuka2
        kuka_2_handle = gym.create_actor(env, kuka_asset, pose_2, "kuka2", i, 1, segmentationId=11)        
        

        # add soft obj        
        env_obj = env
        envs_obj.append(env_obj)        
        
        soft_actor = gym.create_actor(env_obj, soft_asset, soft_pose, "soft", i, 0)

        # add rigid obj
        rigid_actor = gym.create_actor(env, rigid_asset, rigid_pose, 'rigid', i, 0, segmentationId=11)
        color = gymapi.Vec3(1,0,0)
        gym.set_rigid_body_color(env, rigid_actor, 0, gymapi.MESH_VISUAL_AND_COLLISION, color)
        object_handles.append(soft_actor)


        kuka_handles_2.append(kuka_2_handle)



    dof_props_2 = gym.get_actor_dof_properties(envs[0], kuka_handles_2[0])
    dof_props_2["driveMode"].fill(gymapi.DOF_MODE_POS)
    dof_props_2["stiffness"].fill(200.0)
    dof_props_2["damping"].fill(40.0)
    dof_props_2["stiffness"][8:].fill(1)
    dof_props_2["damping"][8:].fill(2)  
    

    # Camera setup
    if not args.headless:
        cam_pos = gymapi.Vec3(1, 0.5, 1)
        cam_pos = gymapi.Vec3(0.3, -0.7, 0.3)
        # cam_pos = gymapi.Vec3(0.3, -0.1, 0.5)  # final setup for thin layer tissue
        # cam_pos = gymapi.Vec3(-0.5, -0.46, 0.4)
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
    cam_positions.append(gymapi.Vec3(0.1, -0.55, 0.15 + 0.081))
    cam_targets.append(gymapi.Vec3(0.035, 0.4-two_robot_offset, 0.02 + 0.081))
    # cam_positions.append(gymapi.Vec3(0.15, 0.5-two_robot_offset, 0.2))
    # cam_targets.append(gymapi.Vec3(0.035, 0.40-two_robot_offset, 0.05))
    
    for i, env_obj in enumerate(envs_obj):
        # for c in range(len(cam_positions)):
            cam_handles.append(gym.create_camera_sensor(env_obj, cam_props))
            gym.set_camera_location(cam_handles[i], env_obj, cam_positions[0], cam_targets[0])



    # set dof properties
    for env in envs:
        gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)

        

    '''
    Main stuff is here
    '''
    rospy.init_node('isaac_grasp_client')


  
 

    # Some important paramters
    init()  # Initilize 2 robots' joints
    all_done = False
    state = "home"
    
    sample_count = 0
    frame_count = 0
    group_count = 0
    data_point_count = 217
    max_group_count = 1500
    max_sample_count = 10
    max_data_point_count = 13000

    final_point_clouds = []
    final_desired_positions = []
    pc_on_trajectory = []
    full_pc_on_trajectory = []
    poses_on_trajectory = []
    first_time = True
    save_intial_pc = True
    switch = True

    dc_client = GraspDataCollectionClient()
    # data_recording_path = "/home/baothach/shape_servo_data/generalization/surgical_setup/data"
    data_path = "/home/baothach/shape_servo_data/generalization/surgical_setup/data_on_ground_2"
    processed_pc_path = "/home/baothach/shape_servo_data/generalization/surgical_setup/keypoint_data/pc"
    processed_img_path = "/home/baothach/shape_servo_data/generalization/surgical_setup/keypoint_data/image"

    # # Load multi object poses:
    # with open('/home/baothach/shape_servo_data/keypoints/combined_w_shape_servo/record_multi_object_poses/batch2(200).pickle', 'rb') as handle:
    #     saved_object_states = pickle.load(handle) 

    
    
    start_time = timeit.default_timer()    

    close_viewer = False

    robot = Robot(gym, sim, envs[0], kuka_handles_2[0])
    # constrain_plane = np.array([-1, 1, 0, 0.45]) 
    # constrain_plane = np.array([0, 1, 0, 0.45]) # 1
    # constrain_plane = np.array([1, 1, 0, 0.4])  # 2
    constrain_plane = np.array([-1, 1, 0, 0.48])  # 3
    # constrain_plane = np.array([-2, 1, 0, 0.45]) # 4  
    # constrain_plane = np.array([-1, 2, 0, 0.9]) # 5  
    # constrain_plane = np.array([1, 2, 0, 0.85]) # 6
    while (not close_viewer) and (not all_done): 



        if not args.headless:
            close_viewer = gym.query_viewer_has_closed(viewer)  

        # step the physics
        gym.simulate(sim)
        gym.fetch_results(sim, True)
 

        if state == "home" :   
            frame_count += 1
            # gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_main_insertion_joint"), 0.103)
            gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_main_insertion_joint"), 0.203)            
            if frame_count == 5:
                if first_time:
                    object_state = gym.get_actor_rigid_body_states(envs[i], object_handles[i], gymapi.STATE_POS)                
                    object_state['pose']['p']['z'] -= 0.05                
                    gym.set_actor_rigid_body_states(envs[i], object_handles[i], object_state, gymapi.STATE_ALL)  
                    first_time = False 
            if frame_count == 10:
                rospy.loginfo("**Current state: " + state + ", current sample count: " + str(sample_count))
          

                state = "generate preshape"
                
                frame_count = 0

                # current_pc = get_point_cloud()
                current_pc = get_partial_point_cloud(i)
                ordered_current_pc = convert_unordered_to_ordered_pc(current_pc, i)
                goal_pc = get_goal_plane(constrain_plane=constrain_plane, initial_pc=current_pc) 
                ordered_goal_pc = convert_unordered_to_ordered_pc(goal_pc, i)
                print("**ordered shapes:", ordered_current_pc.shape, ordered_goal_pc.shape)
                data = {"pc": ordered_current_pc, "pc_goal": ordered_goal_pc}
                with open('/home/baothach/shape_servo_data/generalization/surgical_setup/plane_vis/saved_plane/test_ordered_pc_3.pickle', 'wb') as handle:
                    pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)    

                # for j in range(0, 6000):
                #     with open(os.path.join(data_path, 'sample ' + str(j) + '.pickle'), 'rb') as handle:
                #         data = pickle.load(handle)                    
                #     if j % 50 == 0:
                #         print(j)
                #     # print(data)
                #     ordered_pc, img = convert_unordered_to_ordered_pc(data["partial pcs"][0], i, get_depth_img=True)
                #     ordered_pc_goal, img_goal = convert_unordered_to_ordered_pc(data["partial pcs"][1], i, get_depth_img=True)

                #     # pcd = open3d.geometry.PointCloud()
                #     # pcd.points = open3d.utility.Vector3dVector(np.array(ordered_pc_goal.reshape(-1,3)))
                #     # open3d.visualization.draw_geometries([pcd]) 

                #     image = Image.fromarray(np.uint8(img), mode = 'L')
                #     image.save(os.path.join(processed_img_path, "source", 'sample ' + str(j) + ".png"))
                #     image_goal = Image.fromarray(np.uint8(img_goal), mode = 'L')
                #     image_goal.save(os.path.join(processed_img_path, "target", 'sample ' + str(j) + ".png"))

                #     processed_data = {"pc": ordered_pc, "pc_goal": ordered_pc_goal}
                #     with open(os.path.join(processed_pc_path, "sample " + str(j) + ".pickle"), 'wb') as handle:
                #         pickle.dump(processed_data, handle, protocol=pickle.HIGHEST_PROTOCOL)         

        # step rendering
        gym.step_graphics(sim)
        if not args.headless:
            gym.draw_viewer(viewer, sim, False)
            # gym.sync_frame_time(sim)


  
   



    print("All done !")
    print("Elapsed time", timeit.default_timer() - start_time)
    if not args.headless:
        gym.destroy_viewer(viewer)
    gym.destroy_sim(sim)
    print("total data pt count: ", data_point_count)
