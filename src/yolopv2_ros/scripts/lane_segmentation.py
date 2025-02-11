#!/usr/bin/env python
# -*- coding: utf-8 -*-

import rospy
import argparse
import time
import cv2
import torch
import numpy as np
from pathlib import Path  # 여기서 딱 한 번 import (전역적으로 사용)

from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError

# YOLOPv2 유틸
from utils.utils import (
    time_synchronized,
    select_device,
    increment_path,
    # scale_coords,  # Detection용, 여기서는 사용 안 함
    split_for_trace_model,
    lane_line_mask,
    show_seg_result,
    AverageMeter,
    LoadCamera,
    LoadImages
)

"""
ROS 노드:
 Lane Line 세그멘테이션만 수행 (ROI 밖은 인식 X),
 결과 영상을 ROS Image 토픽으로 퍼블리시,
 필요하면 화면에도 표시 및 rosrun 으로 실행.
"""

def make_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', nargs='+', type=str,
                        default='/home/hannibal/Desktop/yolopv2.pt',
                        help='model.pt path(s)')
    parser.add_argument('--source', type=str,
                        default='0',
                        help='source: 0(webcam) or path to video/image')
    parser.add_argument('--img-size', type=int, default=640,
                        help='inference size (pixels)')
    parser.add_argument('--device', default='0',
                        help='cuda device, i.e. 0 or cpu')

    # 아래 인자들은 Detection용이지만, 우선 잔류
    parser.add_argument('--conf-thres', type=float, default=0.3, help='(unused)')
    parser.add_argument('--iou-thres', type=float, default=0.45, help='(unused)')
    parser.add_argument('--nosave', action='store_false',
                        help='if false => do NOT save images/videos, else => save')
    parser.add_argument('--project', default='runs/detect',
                        help='save results to project/name')
    parser.add_argument('--name', default='exp',
                        help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true',
                        help='existing project/name ok, do not increment')


    return parser


def make_webcam_video(record_frames, save_dir: Path, stem_name: str, real_duration: float):
    """웹캠 모드 시, 누적된 프레임을 실제 걸린 시간에 맞춰 mp4로 저장."""
    if len(record_frames) == 0:
        rospy.loginfo("[INFO] 저장할 웹캠 프레임이 없습니다.")
        return

    num_frames = len(record_frames)
    if real_duration <= 0:
        real_duration = 1e-6
    real_fps = num_frames / real_duration

    rospy.loginfo("[INFO] 웹캠 녹화: 총 %d프레임, 소요 %.2f초 => FPS ~ %.2f",
                  num_frames, real_duration, real_fps)

    save_path = str(save_dir / f"{stem_name}_webcam.mp4")
    h, w = record_frames[0].shape[:2]
    out = cv2.VideoWriter(save_path,
                          cv2.VideoWriter_fourcc(*'mp4v'),
                          real_fps,
                          (w, h))

    for f in record_frames:
        out.write(f)

    out.release()
    rospy.loginfo("[INFO] 웹캠 결과 영상 저장 완료: %s", save_path)


def detect_and_publish(opt, pub_img):
    """
    Lane Line만 세그멘테이션.
    ROI 밖은 검정 처리(모델 입력) & 세그 후처리로 0화.
    결과 영상을 ROS Image로 퍼블리시 & (옵션) .mp4로 저장.
    """
    bridge = CvBridge()

    source, weights = opt.source, opt.weights
    imgsz = opt.img_size
    save_img = not opt.nosave and (isinstance(source, str) and not source.endswith('.txt'))

    # 결과 저장 경로
    save_dir = Path(increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok))
    save_dir.mkdir(parents=True, exist_ok=True)

    inf_time = AverageMeter()

    vid_path = None
    vid_writer = None

    # 모델 로드
    stride = 32
    model = torch.jit.load(weights)
    device = select_device(opt.device)
    half = (device.type != 'cpu')
    model = model.to(device)
    if half:
        model.half()
    model.eval()

    # 데이터 로드
    if source.isdigit():
        rospy.loginfo("[INFO] 웹캠(장치 ID=%s)에서 영상을 받습니다.", source)
        dataset = LoadCamera(source, img_size=imgsz, stride=stride)
    else:
        rospy.loginfo("[INFO] 파일(이미지/동영상): %s 를 불러옵니다.", source)
        dataset = LoadImages(source, img_size=imgsz, stride=stride)

    record_frames = []
    start_time = None

    # GPU warmup
    if device.type != 'cpu':
        model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))

    t0 = time.time()


    # main loop
    for path_item, img, im0s, vid_cap in dataset:
        if dataset.mode == 'stream' and start_time is None:
            start_time = time.time()

        im0_display = im0s.copy()
        im0_det = im0s.copy()



        # 3. convert img (640*640*3 result of phase2) io img_t (640*640*4 numpy tensor) by using letterbox
        img_t = torch.from_numpy(img).to(device)
        img_t = img_t.half() if half else img_t.float()
        img_t /= 255.0
        if img_t.ndimension() == 3:
            img_t = img_t.unsqueeze(0)

        # Inferencing
        t1 = time_synchronized()
        [_, _], seg, ll = model(img_t)
        t2 = time_synchronized()
        # Line lane mask
        ll_seg_mask = lane_line_mask(ll).astype(np.uint8)
        inf_time.update(t2 - t1, img_t.size(0))


        # 세그 시각화
        # 4. display 640*640 into 1280*720 (not resize!!! just display)
        show_seg_result(im0_display, (ll_seg_mask), is_demo=True)
        
        try:
            ros_img = bridge.cv2_to_imgmsg(ll_seg_mask * 255, encoding="mono8") 
            pub_img.publish(ros_img)
        except CvBridgeError as e:
            rospy.logerr("CvBridge Error: %s", str(e))
        

        # (선택) 화면 표시
        cv2.imshow('YOLOPv2 LaneLine with Original Image (ROS)', im0_display)
        cv2.imshow('YOLOPv2 LaneLine Only (ROS)', (ll_seg_mask * 255).astype(np.uint8))
        
        
        if cv2.waitKey(1) & 0xFF == ord('q'):
            rospy.loginfo("[INFO] q 키 입력 -> 종료")
            if isinstance(vid_writer, cv2.VideoWriter):
                vid_writer.release()
            if hasattr(dataset, 'cap') and dataset.cap:
                dataset.cap.release()
            cv2.destroyAllWindows()

            if dataset.mode == 'stream' and save_img:
                end_time = time.time()
                real_duration = end_time - start_time if start_time else 0
                stem_name = Path(path_item).stem if path_item else 'webcam0'
                make_webcam_video(record_frames, save_dir, stem_name, real_duration)
            return

        # 저장
        if dataset.mode == 'image':
            save_path = str(save_dir / Path(path_item).name)
        else:
            save_path = str(save_dir / Path(path_item).stem) + '.mp4'

        if save_img:
            if dataset.mode == 'image':
                sp = Path(save_path)
                if sp.suffix.lower() not in ['.jpg', '.jpeg', '.png', '.bmp']:
                    sp = sp.with_suffix('.jpg')
                cv2.imwrite(str(sp), im0_display)
                rospy.loginfo("Image saved: %s", str(sp))
            elif dataset.mode == 'video':
                nonlocal_vid_path = vid_path
                nonlocal_vid_writer = vid_writer

                if nonlocal_vid_path != save_path:
                    nonlocal_vid_path = save_path
                    if isinstance(nonlocal_vid_writer, cv2.VideoWriter):
                        nonlocal_vid_writer.release()
                    if vid_cap:
                        fps = vid_cap.get(cv2.CAP_PROP_FPS)
                        wv = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        hv = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    else:
                        fps, wv, hv = 30, im0_display.shape[1], im0_display.shape[0]
                    rospy.loginfo("[INFO] 비디오 저장 시작: %s (FPS=%.2f, size=(%d,%d))",
                                  nonlocal_vid_path, fps, wv, hv)
                    nonlocal_vid_writer = cv2.VideoWriter(
                        nonlocal_vid_path,
                        cv2.VideoWriter_fourcc(*'mp4v'),
                        fps,
                        (wv, hv)
                    )
                nonlocal_vid_writer.write(im0_display)
                vid_path = nonlocal_vid_path
                vid_writer = nonlocal_vid_writer
            else:
                # webcam stream
                record_frames.append(im0_display.copy())

    # end for
    if isinstance(vid_writer, cv2.VideoWriter):
        vid_writer.release()
    if hasattr(dataset, 'cap') and dataset.cap:
        dataset.cap.release()
    cv2.destroyAllWindows()

    if dataset.mode == 'stream' and save_img:
        end_time = time.time()
        real_duration = end_time - start_time if start_time else 0
        make_webcam_video(record_frames, save_dir, 'webcam0', real_duration)

    rospy.loginfo("inf : (%.4fs/frame)", inf_time.avg)
    rospy.loginfo("Done. (%.3fs)", (time.time() - t0))


def ros_main():
    rospy.init_node('yolopv2_laneline_node', anonymous=True)

    parser = make_parser()
    opt, _ = parser.parse_known_args()

    pub_img = rospy.Publisher('yolopv2/laneline_image', Image, queue_size=1)

    detect_and_publish(opt, pub_img)

    rospy.loginfo("[INFO] YOLOPv2 LaneLine node finished. spin() for keepalive.")
    rospy.spin()


if __name__ == '__main__':
    try:
        with torch.no_grad():
            ros_main()
    except rospy.ROSInterruptException:
        pass
