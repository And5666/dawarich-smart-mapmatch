# Run with: bin/rails runner scripts/dawarich_loose_tracks.rb [user_id]
# Creates tracks from loose point clusters, then attaches remaining singletons.
class LooseTrackBuilder
  include Tracks::TrackBuilder
  attr_reader :user

  def initialize(user)
    @user = user
  end

  def make(points)
    distance = Point.calculate_distance_for_array_geocoder(points, :m)
    create_track_from_points(points, distance, tracker_id: points.first.tracker_id)
  end
end

user = User.find((ARGV[0] || 5).to_i)
loose = user.points.where(track_id: nil).order(:timestamp).to_a
puts "loose=#{loose.size}"
threshold = user.safe_settings.minutes_between_routes.to_i * 60
groups = []
current = []
loose.each do |point|
  if current.any? && (point.timestamp - current.last.timestamp) > threshold
    groups << current if current.size >= 2
    current = []
  end
  current << point
end
groups << current if current.size >= 2
puts "groups=#{groups.size} points=#{groups.sum(&:size)}"

builder = LooseTrackBuilder.new(user)
created = 0
groups.each do |group|
  begin
    created += 1 if builder.make(group)
  rescue StandardError => e
    puts "FAIL #{group.first.timestamp}..#{group.last.timestamp}: #{e.class}: #{e.message}"
  end
end

tracked = user.points.where.not(track_id: nil).select(:id, :timestamp, :track_id).order(:timestamp, :id).to_a
user.points.where(track_id: nil).order(:timestamp).each do |point|
  Tracks::OrphanPointAttacher.new(user, [point], tracked).call
end
puts "created=#{created} tracks=#{user.tracks.count} loose=#{user.points.where(track_id: nil).count}"
