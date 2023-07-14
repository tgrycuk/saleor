docker build -t my_saleor:v0test .

docker build -f Dockerfile.fc1 -t my_saleor:fc1 .

docker build -f Dockerfile.my -t my_saleor:my .

.devcontainer/Dockerfile

docker-compose -f .devcontainer/docker-compose.yml build
docker-compose -f .devcontainer/docker-compose.yml up
docker-compose -f .devcontainer/docker-compose.yml up -d
docker-compose -f .devcontainer/docker-compose.yml down
docker-compose -f .devcontainer/docker-compose.yml logs -f
docker-compose -f .devcontainer/docker-compose.yml
docker-compose -f .devcontainer/docker-compose.yml


docker-compose -f .devcontainer/docker-compose.yml run saleor bash











