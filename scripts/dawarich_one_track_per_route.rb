# Run with: bin/rails runner scripts/dawarich_one_track_per_route.rb [user_id]
# Rebuilds one Dawarich track per generated route instead of letting the
# background grouper merge spatially distant points that happen to be close in
# time. Requires points pushed with a unique device_id per route.
class RouteTrackBuilder
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
Track.where(user_id: user.id).destroy_all
user.points.update_all(track_id: nil)
groups = user.points.order(:timestamp, :id).group_by(&:tracker_id)
builder = RouteTrackBuilder.new(user)
created = 0
groups.each do |tracker, points|
  next unless tracker.to_s.start_with?('smartroute')

  begin
    created += 1 if builder.make(points)
  rescue StandardError => e
    puts "FAIL #{tracker}: #{e.class}: #{e.message}"
  end
end
user.update_column(:points_count, user.points.count)
(7..10).each { |month| Stats::CalculateMonth.new(user.id, 2026, month, notify_on_failure: false).call }
puts "created=#{created} tracks=#{user.tracks.count} loose=#{user.points.where(track_id: nil).count}"
