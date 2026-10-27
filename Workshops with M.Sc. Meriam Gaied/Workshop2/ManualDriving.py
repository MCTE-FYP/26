import csv
import logging
import math
import queue
import threading
from datetime import datetime
from pathlib import Path
import time

import carla
import pygame

logging.basicConfig(
    filename=str(Path(__file__).with_name("data.txt")),
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger(__name__)

STEERING_AXIS = 0
THROTTLE_AXIS = 2
BRAKE_AXIS = 5
REVERSE_BUTTON = 21
HANDBRAKE_BUTTON = 23

SIMULATION_HZ = 90
FIXED_DELTA_SECONDS = 1.0 / SIMULATION_HZ
SAVED_IMAGE_WIDTH = 640
SAVED_IMAGE_HEIGHT = 360
CAMERA_MAX_HEAD_PITCH_DEG = 1.5
CAMERA_HEAD_RESPONSE_SECONDS = 0.22
CAMERA_MAX_ACCELERATION_MPS2 = 4.0
CAMERA_MAX_VERTICAL_SHIFT_PIXELS = 12
IMAGE_CAPTURE_RATE_HZ = 5
IMAGE_QUEUE_SIZE = 5
SAVE_CAMERA_IMAGES = True

IMAGE_FRAME_INTERVAL = max(1, round(SIMULATION_HZ / IMAGE_CAPTURE_RATE_HZ))
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
IMAGE_DIRECTORY = Path(__file__).with_name("CameraImages") / f"run_{RUN_TIMESTAMP}"
MOTION_CUEING_FILE = Path(__file__).with_name(f"motion_cueing_{RUN_TIMESTAMP}.csv")
MOTION_CUEING_COLUMNS = (
    "acceleration_forward_mps2",
    "acceleration_right_mps2",
    "acceleration_up_mps2",
    "angular_velocity_roll_deg_s",
    "angular_velocity_pitch_deg_s",
    "angular_velocity_yaw_deg_s",
)


def normalize_pedal_axis(axis_value):
    return max(0.0, min(1.0, (axis_value + 1.0) / 2.0))


class CameraRecorder:
    def __init__(self):
        self.sensor = None
        self.writer = None
        self.images = queue.Queue(maxsize=IMAGE_QUEUE_SIZE)
        self.errors = []
        self.dropped = 0
        self.latest_image = None
        self.image_lock = threading.Lock()
        self.head_pitch_degrees = 0.0
        self.last_render_time = None
        self.display_fps = 0.0

    def start(self, world, vehicle):
        display_info = pygame.display.Info()
        self.display_size = (display_info.current_w, display_info.current_h)
        pygame.display.set_mode(self.display_size, pygame.FULLSCREEN)
        pygame.display.set_caption("CARLA Vehicle Camera")
        self.fps_font = pygame.font.Font(None, 32)

        blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
        blueprint.set_attribute("image_size_x", str(self.display_size[0]))
        blueprint.set_attribute("image_size_y", str(self.display_size[1]))
        blueprint.set_attribute("fov", "90")
        blueprint.set_attribute("sensor_tick", str(FIXED_DELTA_SECONDS))
        self.sensor = world.spawn_actor(
            blueprint,
            carla.Transform(
                carla.Location(x=0, y=-0.4, z=1.15),
                carla.Rotation(pitch=0, yaw=0),
            ),
            attach_to=vehicle,
        )
        if SAVE_CAMERA_IMAGES:
            IMAGE_DIRECTORY.mkdir(parents=True, exist_ok=True)
            self.writer = threading.Thread(target=self._save_images, daemon=True)
            self.writer.start()
        self.sensor.listen(self._queue_image)
        logger.info("Using camera sensor attached to vehicle")
        if SAVE_CAMERA_IMAGES:
            logger.info("Saving camera images at up to %s Hz to %s",
                        IMAGE_CAPTURE_RATE_HZ, IMAGE_DIRECTORY)

    def _queue_image(self, image):
        with self.image_lock:
            self.latest_image = image
        if SAVE_CAMERA_IMAGES and image.frame % IMAGE_FRAME_INTERVAL == 0:
            try:
                self.images.put_nowait(image)
            except queue.Full:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 100 == 0:
                    logger.warning("Dropped %s camera images: save queue is full",
                                   self.dropped)

    def render_latest(
        self, forward_acceleration, delta_seconds, steering, throttle, brake,
        handbrake, reverse,
    ):
        with self.image_lock:
            image = self.latest_image
            self.latest_image = None
        if image is not None:
            now = time.perf_counter()
            if self.last_render_time is not None:
                frame_seconds = now - self.last_render_time
                if frame_seconds > 0:
                    measured_fps = 1.0 / frame_seconds
                    self.display_fps = (
                        measured_fps
                        if self.display_fps == 0.0
                        else 0.9 * self.display_fps + 0.1 * measured_fps
                    )
            self.last_render_time = now

            acceleration_ratio = max(
                -1.0,
                min(1.0, forward_acceleration / CAMERA_MAX_ACCELERATION_MPS2),
            )
            target_pitch = acceleration_ratio * CAMERA_MAX_HEAD_PITCH_DEG
            response = 1.0 - math.exp(
                -delta_seconds / CAMERA_HEAD_RESPONSE_SECONDS
            )
            self.head_pitch_degrees += (
                target_pitch - self.head_pitch_degrees
            ) * response
            display = pygame.display.get_surface()
            max_vertical_shift = round(
                display.get_height()
                * CAMERA_MAX_VERTICAL_SHIFT_PIXELS
                / self.display_size[1]
            )
            vertical_shift = round(
                self.head_pitch_degrees
                / CAMERA_MAX_HEAD_PITCH_DEG
                * max_vertical_shift
            )
            surface = pygame.image.frombuffer(
                image.raw_data, (image.width, image.height), "BGRA"
            )
            if max_vertical_shift:
                surface = pygame.transform.scale(
                    surface,
                    (
                        self.display_size[0],
                        self.display_size[1] + 2 * max_vertical_shift,
                    ),
                )
                display.blit(surface, (0, vertical_shift - max_vertical_shift))
            else:
                display.blit(surface, (0, 0))
            status_labels = [
                self.fps_font.render(
                    f"FPS: {self.display_fps:.1f}", True, (255, 255, 255)
                ),
                self.fps_font.render(
                    f"Throttle: {throttle * 100:.0f}%", True, (255, 255, 255)
                ),
                self.fps_font.render(
                    f"Brake: {brake * 100:.0f}%", True, (255, 255, 255)
                ),
                self.fps_font.render(
                    f"Steering: {steering * 100:+.0f}%", True, (255, 255, 255)
                ),
                self.fps_font.render(
                    f"Handbrake: {'ON' if handbrake else 'OFF'}",
                    True,
                    (255, 255, 255),
                ),
                self.fps_font.render(
                    f"Gear: {'Reverse' if reverse else 'Forward'}",
                    True,
                    (255, 255, 255),
                ),
            ]
            status_width = max(label.get_width() for label in status_labels)
            status_height = sum(label.get_height() for label in status_labels)
            status_gap = 4
            status_padding = 8
            status_background = pygame.Surface(
                (
                    status_width + 2 * status_padding,
                    status_height
                    + 2 * status_padding
                    + status_gap * (len(status_labels) - 1),
                ),
                pygame.SRCALPHA,
            )
            status_background.fill((0, 0, 0, 160))
            display.blit(status_background, (12, 12))
            label_y = 12 + status_padding
            for label in status_labels:
                display.blit(label, (12 + status_padding, label_y))
                label_y += label.get_height() + status_gap
            pygame.display.flip()

    def _save_images(self):
        while True:
            image = self.images.get()
            try:
                if image is None:
                    return
                if SAVE_CAMERA_IMAGES:
                    surface = pygame.image.frombuffer(
                        image.raw_data, (image.width, image.height), "BGRA"
                    )
                    surface = pygame.transform.scale(
                        surface, (SAVED_IMAGE_WIDTH, SAVED_IMAGE_HEIGHT)
                    )
                    pygame.image.save(
                        surface,
                        str(IMAGE_DIRECTORY / f"frame_{image.frame:08d}.png"),
                    )
            except Exception as exc:
                self.errors.append(exc)
                logger.exception("Failed to save camera frame %s", image.frame)
            finally:
                self.images.task_done()

    def check(self):
        if self.errors:
            raise RuntimeError("Camera image writer failed") from self.errors[0]

    def close(self):
        try:
            if self.sensor is not None:
                try:
                    self.sensor.stop()
                finally:
                    self.sensor.destroy()
        finally:
            if self.writer is not None:
                self.images.put(None)
                self.writer.join()


pygame.init()
pygame.joystick.init()

vehicle = None
camera_recorder = None
motion_log_file = None
world = None
original_settings = None
try:
    if pygame.joystick.get_count() == 0:
        raise RuntimeError("No joystick detected")

    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    if joystick.get_numaxes() <= max(STEERING_AXIS, THROTTLE_AXIS, BRAKE_AXIS):
        raise RuntimeError(f"{joystick.get_name()} does not have the required axes")
    if joystick.get_numbuttons() <= max(REVERSE_BUTTON, HANDBRAKE_BUTTON):
        raise RuntimeError(
            f"{joystick.get_name()} has {joystick.get_numbuttons()} buttons; "
            "buttons 22 and 23 are required"
        )

    pygame.event.pump()
    logger.info(
        "Using %s: %s axes, %s buttons, %s hats",
        joystick.get_name(),
        joystick.get_numaxes(),
        joystick.get_numbuttons(),
        joystick.get_numhats(),
    )

    client = carla.Client("127.0.0.1", 2000)
    client.set_timeout(10.0)
    world = client.get_world()
    original_settings = world.get_settings()
    synchronous_settings = world.get_settings()
    synchronous_settings.synchronous_mode = True
    synchronous_settings.fixed_delta_seconds = FIXED_DELTA_SECONDS
    world.apply_settings(synchronous_settings)

    blueprints = world.get_blueprint_library()
    blueprint = blueprints.find("vehicle.tesla.model3")

    for spawn_point in world.get_map().get_spawn_points():
        vehicle = world.try_spawn_actor(blueprint, spawn_point)
        if vehicle is not None:
            break
    if vehicle is None:
        raise RuntimeError("Could not find a free vehicle spawn point")

    camera_recorder = CameraRecorder()
    camera_recorder.start(world, vehicle)

    motion_log_file = MOTION_CUEING_FILE.open("w", newline="", encoding="utf-8")
    motion_log = csv.writer(motion_log_file)
    motion_log.writerow(MOTION_CUEING_COLUMNS)
    logger.info("Writing motion cueing data to %s", MOTION_CUEING_FILE)

    handbrake = False
    running = True
    last_log = time.monotonic()
    next_tick = time.monotonic()
    while running:
        next_tick += FIXED_DELTA_SECONDS
        sleep_seconds = next_tick - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        else:
            next_tick = time.monotonic()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif (
                event.type == pygame.JOYBUTTONDOWN
                and event.instance_id == joystick.get_instance_id()
                and event.button == HANDBRAKE_BUTTON
            ):
                handbrake = not handbrake
                logger.info("Handbrake: %s", handbrake)
        if not running:
            break

        steering = max(-1.0, min(1.0, joystick.get_axis(STEERING_AXIS)))
        throttle = normalize_pedal_axis(joystick.get_axis(THROTTLE_AXIS))
        brake = normalize_pedal_axis(joystick.get_axis(BRAKE_AXIS))
        reverse = bool(joystick.get_button(REVERSE_BUTTON))

        camera_recorder.check()

        vehicle.apply_control(
            carla.VehicleControl(
                steer=steering,
                throttle=throttle,
                brake=brake,
                reverse=reverse,
                hand_brake=handbrake,
            )
        )

        frame = world.tick()
        snapshot = world.get_snapshot()
        if snapshot.frame != frame:
            raise RuntimeError(
                f"World snapshot frame {snapshot.frame} does not match tick frame {frame}"
            )
        transform = snapshot.find(vehicle.id).get_transform()
        # Get acceleration in world coordinates (m/s^2).
        acceleration_world = vehicle.get_acceleration()

        # Get angular velocity in world coordinates (degrees/s).
        angular_velocity_world = vehicle.get_angular_velocity()

        # Vehicle-relative forward, right, and up axes.
        forward = transform.get_forward_vector()
        right = transform.get_right_vector()
        up = transform.get_up_vector()

        # Convert acceleration to vehicle-relative coordinates.
        acceleration = (
            acceleration_world.x * forward.x
            + acceleration_world.y * forward.y
            + acceleration_world.z * forward.z,

            acceleration_world.x * right.x
            + acceleration_world.y * right.y
            + acceleration_world.z * right.z,

            acceleration_world.x * up.x
            + acceleration_world.y * up.y
            + acceleration_world.z * up.z,
        )

        camera_recorder.render_latest(
            acceleration[0], FIXED_DELTA_SECONDS, steering, throttle, brake,
            handbrake, reverse,
        )

        # Convert angular velocity to vehicle-relative roll, pitch, yaw.
        angular_velocity = (
            angular_velocity_world.x * forward.x
            + angular_velocity_world.y * forward.y
            + angular_velocity_world.z * forward.z,

            angular_velocity_world.x * right.x
            + angular_velocity_world.y * right.y
            + angular_velocity_world.z * right.z,

            angular_velocity_world.x * up.x
            + angular_velocity_world.y * up.y
            + angular_velocity_world.z * up.z,
        )

        motion_log.writerow(
            (
                acceleration[0],
                acceleration[1],
                acceleration[2],
                angular_velocity[0],
                angular_velocity[1],
                angular_velocity[2],
            )
        )

        now = time.monotonic()
        if now - last_log >= 1.0:
            velocity = vehicle.get_velocity()
            speed_mps = math.sqrt(
                velocity.x * velocity.x
                + velocity.y * velocity.y
                + velocity.z * velocity.z
            )
            logger.info(
                "Speed=%.2f m/s  Velocity=(%.2f, %.2f, %.2f) "
                "steer=%.2f throttle=%.2f brake=%.2f "
                "reverse=%s handbrake=%s",
                speed_mps,
                velocity.x,
                velocity.y,
                velocity.z,
                steering,
                throttle,
                brake,
                reverse,
                handbrake,
            )
            motion_log_file.flush()
            last_log = now
finally:
    try:
        if motion_log_file is not None:
            motion_log_file.close()
    finally:
        try:
            if camera_recorder is not None:
                camera_recorder.close()
        finally:
            try:
                if vehicle is not None:
                    vehicle.destroy()
            finally:
                try:
                    if world is not None and original_settings is not None:
                        world.apply_settings(original_settings)
                finally:
                    pygame.quit()
