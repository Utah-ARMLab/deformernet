#!/usr/bin/env python3
from __future__ import print_function, division, absolute_import


import sys
sys.path.append('/home/baothach/dvrk_shape_servo/src/dvrk_env/dvrk_gazebo_control/src')

import os
import math
import numpy as np
from isaacgym import gymapi
from isaacgym import gymtorch
from isaacgym import gymutil
from copy import copy
import rospy
# from dvrk_gazebo_control.srv import *
from geometry_msgs.msg import PoseStamped, Pose
from GraspDataCollectionClient import GraspDataCollectionClient
import open3d
# from utils import open3d_ros_helper as orh
# from utils import o3dpc_to_GraspObject_msg as o3dpc_GO
# #import pptk
from utils.isaac_utils import isaac_format_pose_to_PoseStamped as to_PoseStamped
from utils.isaac_utils import fix_object_frame
import pickle
from ShapeServo import *
# from sklearn.decomposition import PCA
import timeit
from copy import deepcopy
from fit_plane import *
sys.path.append('/home/baothach/shape_servo_DNN')
# from pointcloud_recon_2 import PointNetShapeServo, PointNetShapeServo2
from pointcloud_recon_2 import PointNetShapeServo3
import torch
from PIL import Image






ROBOT_Z_OFFSET = 0.20
# angle_kuka_2 = -0.4
# init_kuka_2 = 0.15
two_robot_offset = 0.86



def get_current_joint_states(i):
    current_position = gym.get_actor_dof_states(envs[i], kuka_handles[i], gymapi.STATE_POS)['pos']
    # current_position = [x[0] for x in current_position]
    return list(current_position)

def init():
    for i in range(num_envs):
        # Kuka 1
        davinci_dof_states = gym.get_actor_dof_states(envs[i], kuka_handles[i], gymapi.STATE_NONE)
        davinci_dof_states['pos'][4] = 0.05
        davinci_dof_states['pos'][8] = 1.5
        davinci_dof_states['pos'][9] = 0.8
        gym.set_actor_dof_states(envs[i], kuka_handles[i], davinci_dof_states, gymapi.STATE_POS)

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
    return list(point_cloud)

def check_reach_desired_position(i, desired_position, error = 0.01 ):
    '''
    Check if the robot has reached the desired goal positions
    '''
    current_position = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)['pos']
    return np.allclose(current_position, desired_position, rtol=0, atol=error)

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
    # seg_buffer = gym.get_camera_image(sim, envs_obj[i], cam_handles[i], gymapi.IMAGE_SEGMENTATION)

    # Get the camera view matrix and invert it to transform points from camera to world
    # space
    
    vinv = np.linalg.inv(np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], cam_handles[0])))

    # Get the camera projection matrix and get the necessary scaling
    # coefficients for deprojection
    proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
    fu = 2/proj[0, 0]
    fv = 2/proj[1, 1]

    # Ignore any points which originate from ground plane or empty space
    # depth_buffer[seg_buffer == 1] = -10001

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
            if p2[0, 2] > 0.005:
                points.append([p2[0, 0], p2[0, 1], p2[0, 2]])

    return np.array(points).astype('float32')

def get_goal_projected_on_image(goal_pc, i, thickness = 0):
    # proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
    # fu = 2/proj[0, 0]
    # fv = 2/proj[1, 1]
    

    u_s =[]
    v_s = []
    for point in goal_pc:
        point = list(point) + [1]

        point = np.expand_dims(np.array(point), axis=0)

        point_cam_frame = point * np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], vis_cam_handles[0]))
        # print("point_cam_frame:", point_cam_frame)
        # image_coordinates = (gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[0]) * point_cam_frame)
        # print("image_coordinates:",image_coordinates)
        # u_s.append(image_coordinates[1, 0]/image_coordinates[2, 0]*2)
        # v_s.append(image_coordinates[0, 0]/image_coordinates[2, 0]*2)
        # print("fu fv:", fu, fv)
        u_s.append(1/2 * point_cam_frame[0, 0]/point_cam_frame[0, 2])
        v_s.append(1/2 * point_cam_frame[0, 1]/point_cam_frame[0, 2])      
          
    centerU = vis_cam_width/2
    centerV = vis_cam_height/2    
    # print(centerU - np.array(u_s)*cam_width)
    # y_s = (np.array(u_s)*cam_width).astype(int)
    # x_s = (np.array(v_s)*cam_height).astype(int)
    y_s = (centerU - np.array(u_s)*vis_cam_width).astype(int)
    x_s = (centerV + np.array(v_s)*vis_cam_height).astype(int)    

    if thickness != 0:
        new_y_s = deepcopy(list(y_s))
        new_x_s = deepcopy(list(x_s))
        for y, x in zip(y_s, x_s):
            for t in range(1, thickness+1):
                new_y_s.append(max(y-t,0))
                new_x_s.append(max(x-t,0))
                new_y_s.append(max(y-t,0))
                new_x_s.append(min(x+t, vis_cam_height-1))                
                new_y_s.append(min(y+t, vis_cam_width-1))
                new_x_s.append(max(x-t,0))                    
                new_y_s.append(min(y+t, vis_cam_width-1))                
                new_x_s.append(min(x+t, vis_cam_height-1))
        y_s = new_y_s
        x_s = new_x_s
    # print(x_s)
    return x_s, y_s

def visualize_plane(plane_eq, x_range=0.1, y_range=0.5, z_range=0.2,num_pts = 10000):
    plane = []
    for i in range(num_pts):
        x = np.random.uniform(-x_range, x_range)
        z = np.random.uniform(0.03, z_range)
        y = -(plane_eq[0]*x + plane_eq[2]*z + plane_eq[3])/plane_eq[1]
        if -y_range < y < 0:
            plane.append([x, y, z])     
    return plane   


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
        sim_params.substeps = 4
        sim_params.flex.solver_type = 5
        sim_params.flex.num_outer_iterations = 4
        sim_params.flex.num_inner_iterations = 40
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
    asset_root = "../../assets"

    pose = gymapi.Transform()
    pose.p = gymapi.Vec3(0.0, -two_robot_offset, ROBOT_Z_OFFSET)
    #pose.r = gymapi.Quat(-0.707107, 0.0, 0.0, 0.707107)


    pose_2 = gymapi.Transform()
    pose_2.p = gymapi.Vec3(0.0, 0.0, ROBOT_Z_OFFSET)
    # pose_2.p = gymapi.Vec3(0.0, 0.85, ROBOT_Z_OFFSET)
    pose_2.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

    asset_options = gymapi.AssetOptions()
    asset_options.armature = 0.001
    asset_options.fix_base_link = True
    asset_options.thickness = 0.002


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



    
    # Load soft objects' assets
    asset_root = "/home/baothach/sim_data/BigBird/BigBird_urdf_new" # Current directory
    # soft_asset_file = "soft_box/soft_box.urdf"
    
    # soft_asset_file = "3m_high_tack_spray_adhesive.urdf"
    soft_asset_file = "cheez_it_white_cheddar.urdf"
    # soft_asset_file = "cholula_chipotle_hot_sauce.urdf"
    # asset_root = '/home/baothach/sim_data/Bao_objects/urdf'
    # soft_asset_file = "long_bar.urdf"


    soft_pose = gymapi.Transform()
    soft_pose.p = gymapi.Vec3(0.0, 0.50-two_robot_offset, 0.03)
    # soft_pose.r = gymapi.Quat(0.0, 0.0, 0.707107, 0.707107)
    soft_pose.r = gymapi.Quat(0.7071068, 0, 0, 0.7071068)
    soft_thickness = 0.005    # important to add some thickness to the soft body to avoid interpenetrations

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

        # add kuka
        kuka_handle = gym.create_actor(env, kuka_asset, pose, "kuka", i, 1, segmentationId=11)

        # add kuka2
        kuka_2_handle = gym.create_actor(env, kuka_asset, pose_2, "kuka2", i, 1, segmentationId=11)        
        

        # add soft obj        
        env_obj = gym.create_env(sim, env_lower, env_upper, num_per_row)
        envs_obj.append(env_obj)        
        
        soft_actor = gym.create_actor(env_obj, soft_asset, soft_pose, "soft", i, 0)
        object_handles.append(soft_actor)




        kuka_handles.append(kuka_handle)
        kuka_handles_2.append(kuka_2_handle)

    # use position and velocity drive for all dofs; override default stiffness and damping values
    dof_props = gym.get_actor_dof_properties(envs[0], kuka_handles[0])
    dof_props["driveMode"].fill(gymapi.DOF_MODE_POS)
    dof_props["stiffness"][:8].fill(200.0)
    dof_props["damping"][:8].fill(40.0)
    dof_props["stiffness"][8:].fill(1)
    dof_props["damping"][8:].fill(2)

    dof_props_2 = gym.get_actor_dof_properties(envs[0], kuka_handles_2[0])
    dof_props_2["driveMode"].fill(gymapi.DOF_MODE_POS)
    dof_props_2["stiffness"].fill(200.0)
    dof_props_2["damping"].fill(40.0)
    dof_props_2["stiffness"][8:].fill(1)
    dof_props_2["damping"][8:].fill(2)  
    
    # dof_props_2["driveMode"][4].fill(gymapi.DOF_MODE_VEL)
    # dof_props_2["stiffness"][4].fill(0.0)
    # dof_props_2["damping"][4].fill(200.0)

    # Camera setup
    if not args.headless:
        cam_pos = gymapi.Vec3(-1, 0.5, 1)
        cam_target = gymapi.Vec3(0.0, 0.0, 0.1)
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
    cam_positions.append(gymapi.Vec3(0.05, -0.32, 0.25))
    # cam_positions.append(gymapi.Vec3(0.0001, -0.45, 0.22))
    cam_targets.append(gymapi.Vec3(0.0, 0.50-two_robot_offset, 0.00))
    # cam_positions.append(gymapi.Vec3(-0.5, 1.0, 0.5))
    # cam_targets.append(gymapi.Vec3(0.0, 0.4, 0.0))    

    
    for i, env_obj in enumerate(envs_obj):
        # for c in range(len(cam_positions)):
            cam_handles.append(gym.create_camera_sensor(env_obj, cam_props))
            gym.set_camera_location(cam_handles[i], env_obj, cam_positions[0], cam_targets[0])


    # Camera for point cloud setup
    vis_cam_positions = []
    vis_cam_targets = []
    vis_cam_handles = []
    vis_cam_width = 400
    vis_cam_height = 400
    vis_cam_props = gymapi.CameraProperties()
    vis_cam_props.width = vis_cam_width
    vis_cam_props.height = vis_cam_height


    # vis_cam_positions.append(gymapi.Vec3(0.2, -0.45, 0.2))
    vis_cam_positions.append(gymapi.Vec3(-0.1, -0.3, 0.2))
    # vis_cam_positions.append(gymapi.Vec3(-0.13, -0.45, 0.1))   # 6 -1 1 0 0.38
    # vis_cam_positions.append(gymapi.Vec3(-0.11, -0.5, 0.1))
    # vis_cam_positions.append(gymapi.Vec3(0.13, -0.35, 0.1))     #7

    # vis_cam_positions.append(gymapi.Vec3(-0.15, 0.4-two_robot_offset, 0.15)) # 8
    # vis_cam_positions.append(gymapi.Vec3(0.0, -0.55, 0.15))
    # vis_cam_targets.append(gymapi.Vec3(0.0, 0.50-two_robot_offset, 0.00))
    vis_cam_targets.append(gymapi.Vec3(0.0, 0.45-two_robot_offset, 0.05))
    # cam_positions.append(gymapi.Vec3(-0.5, 1.0, 0.5))
    # cam_targets.append(gymapi.Vec3(0.0, 0.4, 0.0))    

    
    for i, env_obj in enumerate(envs_obj):
        # for c in range(len(cam_positions)):
            vis_cam_handles.append(gym.create_camera_sensor(env_obj, vis_cam_props))
            gym.set_camera_location(vis_cam_handles[i], env_obj, vis_cam_positions[0], vis_cam_targets[0])



    # set dof properties
    for env in envs:
        gym.set_actor_dof_properties(env, kuka_handles[i], dof_props)
        gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)

        

    '''
    Main stuff is here
    '''
    rospy.init_node('isaac_grasp_client')


  


    # Some important paramters
    init()  # Initilize 2 robots' joints
    all_done = False
    # main_insertion_handle = gym.find_actor_dof_handle(envs[0], kuka_handles[0], 'psm_main_insertion_joint')
    state = "home"
    # state = "get plan"
    # mode = "positive"
    
    sample_count = 0
    frame_count = 0
    max_sample_count = 1000

    final_point_clouds = []
    final_desired_positions = []
    pc_on_trajectory = []
    poses_on_trajectory = []
    first_time = True
    save_intial_pc = True
    # random_stuff = True
    get_goal_pc = True
    
    
    execute_count = 0
    max_execute_count = 1
    vis_frame_count = 0
    num_image = 0
    start_vis_cam = True
    prepare_vis_cam = True
    prepare_vis_goal_pc = True
    prepare_vis_shift_plane = True
    shift_plane = np.array([])
    goal_pc_numpy = []
    
    dc_client = GraspDataCollectionClient()
    save_path = "/home/baothach/shape_servo_data/new_task/plane_vis/8"
    
    # Load multi object poses:
    with open('/home/baothach/shape_servo_data/keypoints/combined_w_shape_servo/record_multi_object_poses/batch2(200).pickle', 'rb') as handle:
        saved_object_states = pickle.load(handle)

    # Set up DNN:
    device = torch.device("cuda")
    model = PointNetShapeServo3(normal_channel=False)
    # model.load_state_dict(torch.load("/home/baothach/shape_servo_data/keypoints/combined_w_shape_servo/batch3_original_partial_pc/weights/run1/batch_3-epoch 150"))  
    model.load_state_dict(torch.load("/home/baothach/shape_servo_data/keypoints/combined_w_shape_servo/batch3_original_partial_pc/weights_2/run1/batch_3-epoch 122"))
    model.eval()


#    Get goal pc:
    # with open('/home/baothach/shape_servo_data/keypoints/combined_w_shape_servo/goal_data_combined/sample 4.pickle', 'rb') as handle:
    #     data = pickle.load(handle)
    #     goal_pc_numpy = data["partial pcs"][1]
    #     goal_pc_numpy = np.tile([[-0.1, -0.3, 0.5]], (goal_pc_numpy.shape[0],1))
    #     goal_pc = torch.from_numpy(np.swapaxes(goal_pc_numpy,0,1)).float() 
    #     pcd_goal = open3d.geometry.PointCloud()
    #     pcd_goal.points = open3d.utility.Vector3dVector(goal_pc_numpy) 
    #     goal_position = data["positions"]   
    
    
    
    
    
    start_time = timeit.default_timer()    

    close_viewer = False

    # constrain_plane = np.array([-1, 1, 0, 0.38])
    constrain_plane = np.array([0, 1, 0, 0.40])
    shift_plane = np.array([0, 1, 0, 0.40+0.03])
    while (not close_viewer) and (not all_done): 

        if not args.headless:
            close_viewer = gym.query_viewer_has_closed(viewer)  

        # step the physics
        gym.simulate(sim)
        gym.fetch_results(sim, True)
        t = gym.get_sim_time(sim)


        if prepare_vis_cam:
            plane_points = visualize_plane(constrain_plane, num_pts=50000)
            plane_xs, plane_ys = get_goal_projected_on_image(plane_points, i, thickness = 0)
            valid_ind = []
            for t in range(len(plane_xs)):
                if 0 < plane_xs[t] < vis_cam_width and 0 < plane_ys[t] < vis_cam_height:
                    valid_ind.append(t)
            plane_xs = np.array(plane_xs)[valid_ind]
            plane_ys = np.array(plane_ys)[valid_ind]
            


            prepare_vis_cam = False

        if start_vis_cam: 
            if vis_frame_count % 20 == 0:
                gym.render_all_camera_sensors(sim)
                im = gym.get_camera_image(sim, envs_obj[i], vis_cam_handles[0], gymapi.IMAGE_COLOR).reshape((vis_cam_height,vis_cam_width,4))
                # goal_xs, goal_ys = get_goal_projected_on_image(data["full pcs"][1], i, thickness = 1)
                
                im[plane_xs, plane_ys, :] = [0,0,255,255]
                if len(goal_pc_numpy) != 0:
                    if prepare_vis_goal_pc == True:
                        goal_pc_xs, goal_pc_ys = get_goal_projected_on_image(goal_pc_numpy, i, thickness = 1)
                        valid_ind = []
                        for t in range(len(goal_pc_xs)):
                            if 0 < goal_pc_xs[t] < vis_cam_width and 0 < goal_pc_ys[t] < vis_cam_height:
                                valid_ind.append(t)
                        goal_pc_xs = np.array(goal_pc_xs)[valid_ind]
                        goal_pc_ys = np.array(goal_pc_ys)[valid_ind]
                        prepare_vis_goal_pc = False

                    im[goal_pc_xs, goal_pc_ys, :] = [255,0,0,255]


                if shift_plane.size > 0:
                    if prepare_vis_shift_plane == True:
                        shift_plane_points = visualize_plane(shift_plane, num_pt=50000)
                        shift_plane_xs, shift_plane_ys = get_goal_projected_on_image(shift_plane_points, i, thickness = 0)
                        valid_ind = []
                        for t in range(len(shift_plane_xs)):
                            if 0 < shift_plane_xs[t] < vis_cam_width and 0 < shift_plane_ys[t] < vis_cam_height:
                                valid_ind.append(t)
                        shift_plane_xs = np.array(shift_plane_xs)[valid_ind]
                        shift_plane_ys = np.array(shift_plane_ys)[valid_ind]
                        prepare_vis_shift_plane = False
                        

                    im[shift_plane_xs, shift_plane_ys, :] = [0,255,0,255]


                im = Image.fromarray(im)
                
                img_path =  os.path.join(save_path, str(num_image)+"_"+str(gym.get_sim_time(sim))+".png")
                
                im.save(img_path)
                num_image += 1
                # im.save("/home/baothach/Downloads/your_file.png")            
            
            # if vis_frame_count % 10 == 0:
            #     current_pc = get_partial_point_cloud(i)            
            #     pcd = open3d.geometry.PointCloud()
            #     pcd.points = open3d.utility.Vector3dVector(current_pc)  
            #     # open3d.visualization.draw_geometries([pcd, pcd_goal])  
            #     chamfer_dist = np.linalg.norm(np.asarray(pcd_goal.compute_point_cloud_distance(pcd)))
            #     print("chamfer distance: ", chamfer_dist)
            #     final_time.append(deepcopy(gym.get_sim_time(sim)))
            #     final_chamfer.append(chamfer_dist)             

            vis_frame_count += 1


        if state == "home" :   
            frame_count += 1

                              
            if frame_count == 10:
                rospy.loginfo("**Current state: " + state + ", current sample count: " + str(sample_count))
                                    
                gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_main_insertion_joint"), 0.203)
                gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_main_insertion_joint"), 0.203)
                gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_tool_gripper1_joint"), 1.5)
                gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_tool_gripper2_joint"), 1.0) 

                # state = get_new_obj_pose(saved_object_states, num_recorded_poses=200, num_particles_in_obj=1743)
                # idx = 160 #101
                # state = saved_object_states[idx*1743:(idx+1)*1743, :]
                # gym.set_particle_state_tensor(sim, gymtorch.unwrap_tensor(state))                  

                state = "generate preshape"
                frame_count = 0

                current_pc = get_partial_point_cloud(i)
                pcd = open3d.geometry.PointCloud()
                pcd.points = open3d.utility.Vector3dVector(np.array(current_pc))
                open3d.io.write_point_cloud("/home/baothach/shape_servo_data/new_task/test_fit_plane/test_1.pcd", pcd)


                current_pc = get_point_cloud()
                pcd = open3d.geometry.PointCloud()
                pcd.points = open3d.utility.Vector3dVector(np.array(current_pc))
                open3d.io.write_point_cloud("/home/baothach/shape_servo_data/multi_grasps/1.pcd", pcd) # save_grasp_visual_data , point cloud of the object
                pc_ros_msg = dc_client.seg_obj_from_file_client(pcd_file_path = "/home/baothach/shape_servo_data/multi_grasps/1.pcd", align_obj_frame = False).obj
                pc_ros_msg = fix_object_frame(pc_ros_msg)

                goal_pc_numpy = get_goal_plane(constrain_plane=constrain_plane, initial_pc=get_partial_point_cloud(i))
        
        if state == "generate preshape":                   
             
            cartesian_goal = None
            preshape_response = dc_client.gen_grasp_preshape_client(pc_ros_msg)               
            for idx in range(len(preshape_response.palm_goal_pose_world)):  # Pick only top grasp
                if preshape_response.is_top_grasp[idx] == True:
                    cartesian_goal = deepcopy(preshape_response.palm_goal_pose_world[idx].pose) # Need fix
                    # dc_clients[i][j].top_grasp_preshape_idx = idx
            if cartesian_goal == None:
                state = "reset"
                rospy.logerr('NO CARTESIAN GOAL.\n') 
                
            else:
                # cartesian_goal.position.x = 0.047#0.023#0.064#-0.072#-0.081#-0.1134
                # cartesian_goal.position.y = -0.239#-0.252#-0.238#-0.341#-0.341#-0.33
                # cartesian_goal.position.z = 0.032#0.032#0.027#0.023#0.024#0.0295  
                
                # cartesian_goal.position.x = 0.075046243#0.032465#0.075046243#-0.0676719#-0.04733923#-0.1134
                # cartesian_goal.position.y = -0.263525#-0.26031256#-0.263525#-0.3105266#-0.3336753#-0.33
                # cartesian_goal.position.z = 0.027#0.032#0.023#0.024#0.0295 
                print("teo:", cartesian_goal.position.x, cartesian_goal.position.y, cartesian_goal.position.z)

                cartesian_goal.position.x = -cartesian_goal.position.x
                cartesian_goal.position.y = -cartesian_goal.position.y
                cartesian_goal.position.z -= (ROBOT_Z_OFFSET)
                cartesian_goal.orientation.x = 0
                cartesian_goal.orientation.y = 0.707107
                cartesian_goal.orientation.z = 0.707107
                cartesian_goal.orientation.w = 0                

                # Get plan from MoveIt
                dc_client.plan_traj = dc_client.arm_moveit_planner_client(go_home=False, cartesian_goal=cartesian_goal, current_position=get_current_joint_states(i))

                # Does plan exist?
                if (not dc_client.plan_traj):
                    rospy.logerr('Can not find moveit plan to grasp. Ignore this grasp.\n')  
                    state = "reset"
                else:
                    rospy.loginfo('Sucesfully found a PRESHAPE moveit plan to grasp.\n')
                    state = "move to preshape"
                    rospy.loginfo('Moving to this preshape goal: ' + str(cartesian_goal))


        if state == "move to preshape":            
            plan_traj_with_gripper = [plan+[1.5,0.8] for plan in dc_client.plan_traj]
            pos_targets = np.array(plan_traj_with_gripper[dc_client.traj_index], dtype=np.float32)
            gym.set_actor_dof_position_targets(envs[i], kuka_handles_2[i], pos_targets)        
            
            dof_states = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)
            
            if np.allclose(dof_states['pos'][:8], pos_targets[:8], rtol=0, atol=0.03) and np.allclose(dof_states['pos'][8:], pos_targets[8:], rtol=0, atol=0.1)  :
                dc_client.traj_index += 1             
            
            if dc_client.traj_index == len(dc_client.plan_traj):
                dc_client.traj_index = 0
                state = "generate preshape 2"   
                rospy.loginfo("Succesfully executed PRESHAPE moveit arm plan. Let's fucking grasp it!!")
                # current_pc = get_partial_point_cloud(i)
                # pcd = open3d.geometry.PointCloud()
                # pcd.points = open3d.utility.Vector3dVector(np.array(current_pc))
                # open3d.io.write_point_cloud("/home/baothach/shape_servo_data/new_task/test_fit_plane/test_1.pcd", pcd)                

        if state == "generate preshape 2":       

            cartesian_goal = None
            preshape_response = dc_client.gen_grasp_preshape_client(pc_ros_msg, non_random = True)               
            for idx in range(len(preshape_response.palm_goal_pose_world)):  # Pick only top grasp
                if preshape_response.is_top_grasp[idx] == True:
                    cartesian_goal = deepcopy(preshape_response.palm_goal_pose_world[idx].pose) # Need fix
                    # dc_clients[i][j].top_grasp_preshape_idx = idx
            if cartesian_goal == None:
                state = "reset"
                rospy.logerr('NO CARTESIAN GOAL.\n') 
                
            else:
                cartesian_goal.position.x = cartesian_goal.position.x
                cartesian_goal.position.y = cartesian_goal.position.y + two_robot_offset
                cartesian_goal.position.z -= ROBOT_Z_OFFSET
                cartesian_goal.orientation.x = 0
                cartesian_goal.orientation.y = 0.707107
                cartesian_goal.orientation.z = 0.707107
                cartesian_goal.orientation.w = 0                

                # Get plan from MoveIt
                dc_client.plan_traj = dc_client.arm_moveit_planner_client(go_home=False, cartesian_goal=cartesian_goal, current_position=get_current_joint_states(i))

                # Does plan exist?
                if (not dc_client.plan_traj):
                    rospy.logerr('Can not find moveit plan to grasp. Ignore this grasp.\n')  
                    state = "reset"
                else:
                    rospy.loginfo('Sucesfully found a PRESHAPE moveit plan to grasp.\n')
                    state = "move to preshape 2"
                    rospy.loginfo('Moving to this preshape goal: ' + str(cartesian_goal))


        if state == "move to preshape 2":            
            plan_traj_with_gripper = [plan+[1.5,0.8] for plan in dc_client.plan_traj]
            pos_targets = np.array(plan_traj_with_gripper[dc_client.traj_index], dtype=np.float32)
            gym.set_actor_dof_position_targets(envs[i], kuka_handles[i], pos_targets)        
            
            dof_states = gym.get_actor_dof_states(envs[i], kuka_handles[i], gymapi.STATE_POS)
            
            if np.allclose(dof_states['pos'][:8], pos_targets[:8], rtol=0, atol=0.1) and np.allclose(dof_states['pos'][8:], pos_targets[8:], rtol=0, atol=0.1)  :
                dc_client.traj_index += 1             
            
            if dc_client.traj_index == len(dc_client.plan_traj):
                dc_client.traj_index = 0
                state = "grasp object"   
                rospy.loginfo("Succesfully executed PRESHAPE moveit arm plan. Let's fucking grasp it!!")


        
        if state == "grasp object":             
            rospy.loginfo("**Current state: " + state)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper1_joint"), -1.5)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper2_joint"), -2) 
            gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_tool_gripper1_joint"), 0.4)
            gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_tool_gripper2_joint"), -0.3)            

            dof_states = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)
            if dof_states['pos'][8] < 0.2:
                                       
                state = "get shape servo plan"
                    
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper1_joint"), 0.15)
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper2_joint"), -0.15)         
        
                anchor_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])
                start_vis_cam = True
                # print("***Current x, y, z: ", current_pose["pose"]["p"]["x"], current_pose["pose"]["p"]["y"], current_pose["pose"]["p"]["z"] )
                # current_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])


        if state == "get shape servo plan":
            rospy.loginfo("**Current state: " + state)

            current_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])
            print("***Current x, y, z: ", current_pose["pose"]["p"]["x"], current_pose["pose"]["p"]["y"], current_pose["pose"]["p"]["z"] ) 
            # gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_tool_gripper1_joint"), 0.4)
            # gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_tool_gripper2_joint"), -0.3) 

            # current_pc = get_point_cloud()
            # current_pc = get_point_cloud_2(i)
            current_pc = get_partial_point_cloud(i)
            
            # pcd = open3d.geometry.PointCloud()
            # pcd.points = open3d.utility.Vector3dVector(current_pc)  
            # # open3d.visualization.draw_geometries([pcd, pcd_goal])  
            # chamfer_dist = np.linalg.norm(np.asarray(pcd_goal.compute_point_cloud_distance(pcd)))
            # print("chamfer distance: ", chamfer_dist)
            # chamfer_distances.append(chamfer_dist)              
            if save_intial_pc:
                initial_pc = deepcopy(current_pc)
                # intial_pc_tensor = torch.from_numpy(np.swapaxes(intial_pc,0,1)).float() 
                save_initial_pc = False
            
            if get_goal_pc:
                # goal_pc_numpy = np.tile([[0.2, -0.2, 0.1]], (current_pc.shape[0],1))
                # goal_pc_numpy = np.array([[-0.2, x[1], x[2]] for x in current_pc])
                # goal_pc_numpy = np.array([[x[0], x[1]+0.2, x[2]] for x in goal_pc_numpy])
                # goal_pc_numpy = []
                # x_range = [-0.0001, -0.0]
                # y_range = [-0.4, -0.3]
                # z_range = [0.1, 0.2]
                # for p in range(current_pc.shape[0]):
                #     x_p = np.random.uniform(low = x_range[0], high = x_range[1])
                #     y_p = np.random.uniform(low = y_range[0], high = y_range[1])
                #     z_p = np.random.uniform(low = z_range[0], high = z_range[1])
                #     goal_pc_numpy.append([x_p, y_p, z_p])
                # goal_pc_numpy = np.array(goal_pc_numpy)    

                # goal_pc = open3d.io.read_point_cloud("/home/baothach/shape_servo_data/new_task/test_fit_plane/goal_1.pcd")
                # goal_pc_numpy = np.array(goal_pc.points)
                # constrain_plane = np.array([-1, 1, 0, 0.37])
                delta = 0.00
                goal_pc_numpy = get_goal_plane(constrain_plane=constrain_plane, initial_pc=initial_pc)  
                           


                goal_pc = torch.from_numpy(np.swapaxes(goal_pc_numpy,0,1)).float() 
                pcd_goal = open3d.geometry.PointCloud()
                pcd_goal.points = open3d.utility.Vector3dVector(goal_pc_numpy) 
                get_goal_pc = False

            full_pc = get_point_cloud()
            # mini = min([x[1] for x in full_pc])
            # maxi = max([x[1] for x in full_pc])
            # print("=========================")
            # print("minimum x:", min([x[0] for x in full_pc]), max([x[0] for x in full_pc]))
            # print("minimum y:", min([x[1] for x in full_pc]), max([x[1] for x in full_pc]))
            # print("minimum z:", min([x[2] for x in full_pc]), max([x[2] for x in full_pc]))
            # print("=========================")

            current_pc = torch.from_numpy(np.swapaxes(current_pc,0,1)).float()         

            # grasp_pose = torch.tensor(list(anchor_pose['pose']['p'])).float().unsqueeze(0)

            with torch.no_grad():
                desired_position = model(current_pc.unsqueeze(0), goal_pc.unsqueeze(0))[0].detach().numpy()*(0.001)  
                # desired_position = model(intial_pc_tensor.unsqueeze(0), goal_pc.unsqueeze(0))[0].detach().numpy()*(0.001) 

            print("from model:", desired_position)
          
            delta_x = desired_position[0]   
            delta_y = desired_position[1] 
            delta_z = desired_position[2] 

            # delta_x = 0#0.4  
            # delta_y = 0#0.3 
            # delta_z = 0.2            

            cartesian_pose = Pose()
            cartesian_pose.orientation.x = 0
            cartesian_pose.orientation.y = 0.707107
            cartesian_pose.orientation.z = 0.707107
            cartesian_pose.orientation.w = 0
            cartesian_pose.position.x = -current_pose["pose"]["p"]["x"] + delta_x
            cartesian_pose.position.y = -current_pose["pose"]["p"]["y"] + delta_y
            # cartesian_pose.position.z = current_pose["pose"]["p"]["z"] - ROBOT_Z_OFFSET + delta_z
            cartesian_pose.position.z = max(0.005- ROBOT_Z_OFFSET,current_pose["pose"]["p"]["z"] - ROBOT_Z_OFFSET + delta_z)
            dof_states = gym.get_actor_dof_states(envs[0], kuka_handles_2[0], gymapi.STATE_POS)['pos']

            plan_traj = dc_client.arm_moveit_planner_client(go_home=False, cartesian_goal=cartesian_pose, current_position=dof_states)
            state = "move to goal"
            traj_index = 0

        if state == "move to goal":           
            # Does plan exist?
            if (not plan_traj):
                rospy.logerr('Can not find moveit plan to grasp. Ignore this grasp.\n')  
                state = "get shape servo plan"
            else:            
                # print(traj_index, len(plan_traj))
                dof_states = gym.get_actor_dof_states(envs[0], kuka_handles_2[0], gymapi.STATE_POS)['pos']
                plan_traj_with_gripper = [plan+[0.15,-0.15] for plan in plan_traj]
                pos_targets = np.array(plan_traj_with_gripper[traj_index], dtype=np.float32)
                gym.set_actor_dof_position_targets(envs[0], kuka_handles_2[0], pos_targets)                
                
                if np.allclose(dof_states[:8], pos_targets[:8], rtol=0, atol=0.01):
                    traj_index += 1 

        
                # if traj_index == 4 or traj_index == len(plan_traj):
                if traj_index == len(plan_traj):
                    traj_index = 0  
                    final_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])
                    # print("***Final x, y, z: ", final_pose[" pose"]["p"]["x"], final_pose["pose"]["p"]["y"], final_pose["pose"]["p"]["z"] ) 
                    delta_x = -(final_pose["pose"]["p"]["x"] - anchor_pose["pose"]["p"]["x"])
                    delta_y = -(final_pose["pose"]["p"]["y"] - anchor_pose["pose"]["p"]["y"])
                    delta_z = final_pose["pose"]["p"]["z"] - anchor_pose["pose"]["p"]["z"]
                    print("delta x, y, z:", delta_x, delta_y, delta_z)
                    
                    state = "get shape servo plan" 
                    execute_count += 1

                    if execute_count >= max_execute_count:
                        rospy.logwarn("Shift goal plane")
                        goal_pc_numpy = get_goal_plane(constrain_plane=constrain_plane, initial_pc=initial_pc, check=True, delta=delta, current_pc=get_partial_point_cloud(i))
                        # goal_pc_numpy = get_goal_plane(constrain_plane=constrain_plane, initial_pc=initial_pc, check=True, delta=delta, current_pc=[])
                        delta += 0.02
                        # delta = min(0.06, delta)
                        shift_plane[3] = constrain_plane[3] + 0.03 + delta
                        prepare_vis_goal_pc = True
                        prepare_vis_shift_plane = True
                        if goal_pc_numpy == 'success':
                            print("=====================SUCCESS================")
                            state = "get shape servo plan aaxa"
                        else:
                            goal_pc = torch.from_numpy(np.swapaxes(goal_pc_numpy,0,1)).float() 
                            pcd_goal = open3d.geometry.PointCloud()
                            pcd_goal.points = open3d.utility.Vector3dVector(goal_pc_numpy)    
                            execute_count = 0
                            state = "get shape servo plan" 
                                   
                    
          

        # print("still going ..")

        if state == "reset":   
            rospy.loginfo("**Current state: " + state) 
            gym.set_particle_state_tensor(sim, gymtorch.unwrap_tensor(saved_object_state))
            pos_targets = np.array([0.,0.,0.,0.,0.05,0.,0.,0.,1.5,0.8], dtype=np.float32)
            gym.set_actor_dof_position_targets(envs[i], kuka_handles[i], pos_targets)
            gym.set_actor_dof_position_targets(envs[i], kuka_handles_2[i], pos_targets)
            dof_states_1 = gym.get_actor_dof_states(envs[0], kuka_handles[0], gymapi.STATE_POS)['pos']
            dof_states_2 = gym.get_actor_dof_states(envs[0], kuka_handles_2[0], gymapi.STATE_POS)['pos']
            if np.allclose(dof_states_1, pos_targets, rtol=0, atol=0.1) and np.allclose(dof_states_2, pos_targets, rtol=0, atol=0.1):
                # print("Scuesfully reset robot")
                gym.set_particle_state_tensor(sim, gymtorch.unwrap_tensor(saved_object_state))
                print("Scuesfully reset robot and object")
                pc_on_trajectory = []
                poses_on_trajectory = []
                state = "home"
 
  
   
                
                
                
        # if sample_count % 5 == 0:
        #     data = {"point clouds": final_point_clouds, "positions": final_desired_positions, "saved intial state":saved_object_state}
        #     with open('/home/baothach/shape_servo_data/multi_grasps/one_grasp', 'wb') as handle:
        #         pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)            

        
        if sample_count == max_sample_count:             
            all_done = True    

        # step rendering
        gym.step_graphics(sim)
        if not args.headless:
            gym.draw_viewer(viewer, sim, False)
            # gym.sync_frame_time(sim)


  
    # data = {"point clouds": final_point_clouds, "positions": final_desired_positions, "saved intial state":saved_object_state}
    # with open('/home/baothach/shape_servo_data/multi_grasps/one_grasp', 'wb') as handle:
    #     pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)

    # data = {"init pc": initial_pc, "final pc": goal_pc_numpy, "change": point_cloud_change}
    # with open('/home/baothach/shape_servo_data/point_cloud_change/change_2', 'wb') as handle:
    #     pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)

    print("All done !")
    print("Elapsed time", timeit.default_timer() - start_time)
    if not args.headless:
        gym.destroy_viewer(viewer)
    gym.destroy_sim(sim)

