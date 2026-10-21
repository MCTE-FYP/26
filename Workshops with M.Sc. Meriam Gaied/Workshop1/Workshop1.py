import pygame

pygame.init()

#initialise the joystick module
pygame.joystick.init()

#define screen size
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 500

#create game window
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Joysticks")

#define font
font_size = 30
font = pygame.font.SysFont("Futura", font_size)

#function for outputting text onto the screen
def draw_text(text, font, text_col, x, y):
  img = font.render(text, True, text_col)
  screen.blit(img, (x, y))

#create clock for setting game frame rate
clock = pygame.time.Clock()
FPS = 60

#create empty list to store joysticks
joysticks = []

#create player rectangle
x = 350
y = 200
player = pygame.Rect(x, y, 100, 100)

#define player colour
col = "royalblue"

#define movement speed
speed = 5

#game loop
run = True
while run:

  clock.tick(FPS)

  #update background
  screen.fill(pygame.Color("midnightblue"))

  #draw player
  player.topleft = (x, y)
  pygame.draw.rect(screen, pygame.Color(col), player)

  #event handler
  for event in pygame.event.get():
    if event.type == pygame.JOYDEVICEADDED:
      joy = pygame.joystick.Joystick(event.device_index)
      joysticks.append(joy)
    #quit program
    if event.type == pygame.QUIT:
      run = False

  #show number of connected joysticks
  if pygame.joystick.get_count() > 0:
    draw_text("Controllers: " + str(pygame.joystick.get_count()), font, pygame.Color("azure"), 10, 10)
    draw_text("Battery Level: " + str(joy.get_power_level()), font, pygame.Color("azure"), 10, 35)
    draw_text("Controller Type: " + str(joy.get_name()), font, pygame.Color("azure"), 10, 60)
    draw_text("Number of axes: " + str(joy.get_numaxes()), font, pygame.Color("azure"), 10, 85)

  #handle joystick input
  for joystick in joysticks:
    #change player colour with buttons
    if joystick.get_button(2):
      col = "royalblue"
    if joystick.get_button(1):
      col = "crimson"
    if joystick.get_button(3):
      col = "yellow"
    if joystick.get_button(0):
      col = "forestgreen"
    
    #player movement with hat (D-pad)
    hat = joystick.get_hat(0)
    if hat[0] == -1:  # Left
      x -= speed
    if hat[0] == 1:   # Right
      x += speed
    if hat[1] == 1:   # Up
      y -= speed
    if hat[1] == -1:  # Down
      y += speed
    
    #keep player within screen boundaries
    if x < 0:
      x = 0
    if x > SCREEN_WIDTH - 100:
      x = SCREEN_WIDTH - 100
    if y < 0:
      y = 0
    if y > SCREEN_HEIGHT - 100:
      y = SCREEN_HEIGHT - 100

  #update display
  pygame.display.flip()

pygame.quit()



#no need for main function or if __name__ == "__main__" guard
